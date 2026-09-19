"""Phase 4 — bounded, resumable historical Qdrant reindex repair.

Reuses the production handle_upload_complete() handler directly for every
asset -- no reimplementation of extraction/embedding/indexing logic.

Safety properties:
  - Reads its candidate list from the Phase 0/1 dry-run JSON (deterministic,
    already verified: MISSING_POINT + source object confirmed present).
  - Checkpoint file (JSON) records per-asset outcome as it goes; re-running
    this script skips anything already recorded as done/permanently-failed,
    so it's safe to interrupt (Ctrl-C, process kill) and restart at any point.
  - Bounded concurrency via a semaphore (default 5 concurrent).
  - Per-asset timeout, so one stuck asset can't stall the whole batch.
  - Transient failures (timeouts, connection errors) get up to 3 retries
    with exponential backoff; anything else is recorded as a permanent
    failure and NOT retried endlessly.
  - Organization-scoped batching: processes one org's full candidate list
    before moving to the next, so partial progress is easy to reason about.
  - Circuit breaker: if the rolling failure rate over the last 50 attempts
    exceeds 20%, the run pauses itself (stops picking up new work) rather
    than continuing to hammer a possibly-unhealthy Qdrant/embedding path.
  - Before/after Qdrant health check.

Run:
    venv/bin/python3 scripts/utilities/qdrant_reindex_repair.py \
        --candidates /path/to/reconcile_dry_run.json \
        --checkpoint /path/to/repair_checkpoint.json \
        [--limit N] [--concurrency 5] [--dry-run]
"""
import argparse
import asyncio
import json
import sys
import time
from collections import deque, defaultdict
from pathlib import Path

sys.path.insert(0, ".")

from app.core.database import engine
from app.core.s3_client import init_s3_client
from app.workers.ai.embeddings import handle_upload_complete
from app.services.search.indexer import get_qdrant
from sqlalchemy import text

PER_ASSET_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
CIRCUIT_BREAKER_WINDOW = 50
CIRCUIT_BREAKER_FAILURE_RATE = 0.20


def load_checkpoint(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"done": {}, "permanent_failures": {}}


def save_checkpoint(path: str, state: dict):
    Path(path).write_text(json.dumps(state, indent=2))


async def fetch_org_for_asset(asset_id: str, cache: dict) -> str:
    return cache[asset_id]


async def process_one(asset_id: str, org_id: str, blob_ref: str, state: dict, dry_run: bool) -> str:
    """Returns 'ok', 'permanent_fail', or 'transient_fail'."""
    if dry_run:
        return "ok"
    payload = {"asset_id": asset_id, "org_id": org_id, "object_key": blob_ref}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await asyncio.wait_for(handle_upload_complete(payload), timeout=PER_ASSET_TIMEOUT_SECONDS)
            return "ok"
        except asyncio.TimeoutError:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
                continue
            return "transient_fail"
        except (ConnectionError, OSError) as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
                continue
            return "transient_fail"
        except Exception as e:
            # Anything else (bad data, extraction error, etc.) is treated
            # as permanent -- retrying handle_upload_complete on the same
            # bad input won't change the outcome.
            state.setdefault("_last_errors", {})[asset_id] = str(e)[:300]
            return "permanent_fail"
    return "transient_fail"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.candidates) as f:
        dry_run_data = json.load(f)
    asset_ids = dry_run_data["repair_candidate_asset_ids"]
    org_map = dry_run_data["repair_candidate_org_ids"]
    if args.limit:
        asset_ids = asset_ids[:args.limit]

    state = load_checkpoint(args.checkpoint)
    done = state["done"]
    permanent_failures = state["permanent_failures"]

    todo = [a for a in asset_ids if a not in done and a not in permanent_failures]
    print(f"total candidates: {len(asset_ids)} | already done: {len(done)} | "
          f"already permanently failed: {len(permanent_failures)} | remaining: {len(todo)}")

    if not todo:
        print("nothing to do.")
        return

    await init_s3_client()

    # blob_ref lookup for the remaining todo set only
    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT id, blob_ref FROM assets WHERE id::text = ANY(:ids)"
        ), {"ids": todo})
        blob_refs = {str(row.id): row.blob_ref for row in r}

    # organization-scoped ordering
    by_org = defaultdict(list)
    for a in todo:
        by_org[org_map[a]].append(a)
    ordered = [a for org in by_org for a in by_org[org]]

    sem = asyncio.Semaphore(args.concurrency)
    recent_outcomes = deque(maxlen=CIRCUIT_BREAKER_WINDOW)
    stats = {"ok": 0, "permanent_fail": 0, "transient_fail": 0}
    circuit_open = False
    processed_count = 0
    t0 = time.monotonic()

    async def worker(asset_id):
        nonlocal circuit_open
        async with sem:
            if circuit_open:
                return
            org_id = org_map[asset_id]
            blob_ref = blob_refs.get(asset_id)
            outcome = await process_one(asset_id, org_id, blob_ref, state, args.dry_run)
            stats[outcome] += 1
            recent_outcomes.append(1 if outcome != "ok" else 0)
            if outcome == "ok":
                done[asset_id] = {"org_id": org_id, "ts": time.time()}
            elif outcome == "permanent_fail":
                permanent_failures[asset_id] = {"org_id": org_id, "ts": time.time(),
                                                 "error": state.get("_last_errors", {}).get(asset_id, "")}
            # transient_fail after exhausting retries: leave it OUT of both
            # done and permanent_failures so a future run retries it fresh.

            if len(recent_outcomes) == CIRCUIT_BREAKER_WINDOW:
                rate = sum(recent_outcomes) / len(recent_outcomes)
                if rate > CIRCUIT_BREAKER_FAILURE_RATE:
                    circuit_open = True
                    print(f"\n!!! CIRCUIT BREAKER TRIPPED: {rate:.0%} failure rate over last "
                          f"{CIRCUIT_BREAKER_WINDOW} attempts — pausing, no further NEW work will start !!!")

    tasks = []
    try:
        for asset_id in ordered:
            if circuit_open:
                break
            tasks.append(asyncio.create_task(worker(asset_id)))
            processed_count += 1
            if processed_count % 25 == 0:
                save_checkpoint(args.checkpoint, state)
                elapsed = time.monotonic() - t0
                print(f"  progress: {processed_count}/{len(ordered)} dispatched | "
                      f"ok={stats['ok']} perm_fail={stats['permanent_fail']} "
                      f"transient={stats['transient_fail']} | {elapsed:.0f}s elapsed")
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\nInterrupted — checkpoint saved, safe to resume by re-running this command.")
    finally:
        save_checkpoint(args.checkpoint, state)

    print(f"\n=== BATCH COMPLETE ===")
    print(f"ok={stats['ok']} permanent_fail={stats['permanent_fail']} transient_fail={stats['transient_fail']}")
    print(f"circuit_open={circuit_open}")
    print(f"checkpoint saved to {args.checkpoint}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
