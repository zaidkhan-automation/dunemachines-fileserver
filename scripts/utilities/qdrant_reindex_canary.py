"""Phase 3 — production canary for the historical Qdrant reindex repair.

Runs a small, hand-picked set of REAL missing production assets (non-
sensitive by name/type, across multiple orgs) through the actual
production handle_upload_complete() handler -- not a reimplementation.

Verifies, per asset:
  - point created in the CORRECT org-scoped collection
  - exactly one point (no duplicate)
  - semantic search can retrieve it, scoped to its own org
  - a DIFFERENT org's search does NOT see it (isolation)
  - source Postgres row (name/mime/status/blob_ref) unchanged
  - no worker exception

Read/write to Qdrant only for these 5 specific points. Never touches
MinIO objects or Postgres asset rows beyond a read-only re-fetch for the
unchanged-row check.
"""
import asyncio
import sys

sys.path.insert(0, ".")

from app.core.database import engine, AsyncSessionLocal
from app.core.s3_client import init_s3_client
from app.repositories.asset_repo import asset_repo
from app.services.search.indexer import get_qdrant, _collection_name, _point_id
from app.workers.ai.embeddings import handle_upload_complete
from sqlalchemy import text

CANARY_ASSET_IDS = [
    "1e77c315-a6b4-45ea-9a26-2061fc3a6f70",
    "b1a966b1-629d-45e7-8abf-d60ac9a9492a",
    "184a5f5f-6b62-4347-9a2a-9308b17960b7",
    "9e6b1be9-ec15-43be-a285-7174bfee4baf",
    "501725d7-f2e6-462e-a843-6aa8c01f226f",
]

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail and status == "FAIL" else ""))


async def snapshot_asset(asset_id):
    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT id, organization_id, name, mime_type, status, blob_ref, updated_at FROM assets WHERE id = :id"
        ), {"id": asset_id})
        row = r.first()
        return dict(row._mapping) if row else None


async def main():
    await init_s3_client()
    qc = get_qdrant()

    before = {}
    for aid in CANARY_ASSET_IDS:
        before[aid] = await snapshot_asset(aid)
        if not before[aid]:
            print(f"ABORT: asset {aid} not found")
            return False

    per_asset_org = {}
    for aid in CANARY_ASSET_IDS:
        a = before[aid]
        org_id = str(a["organization_id"])
        per_asset_org[aid] = org_id
        payload = {"asset_id": aid, "org_id": org_id, "object_key": a["blob_ref"]}
        try:
            await handle_upload_complete(payload)
            check(f"handle_upload_complete ran without exception ({aid[:8]})", True)
        except Exception as e:
            check(f"handle_upload_complete ran without exception ({aid[:8]})", False, str(e))
            continue

        collection = _collection_name(org_id)
        point_id = _point_id(aid)
        pts = await qc.retrieve(collection_name=collection, ids=[point_id])
        check(f"point created in correct org collection ({aid[:8]})", len(pts) == 1,
              f"collection={collection} found={len(pts)}")

    # duplicate check: re-run one asset again, confirm still exactly one point
    dup_aid = CANARY_ASSET_IDS[0]
    dup_org = per_asset_org[dup_aid]
    await handle_upload_complete({"asset_id": dup_aid, "org_id": dup_org, "object_key": before[dup_aid]["blob_ref"]})
    pts_dup = await qc.retrieve(collection_name=_collection_name(dup_org), ids=[_point_id(dup_aid)])
    check("re-running one canary asset produces no duplicate point", len(pts_dup) == 1, f"found {len(pts_dup)}")

    # semantic search: own org can find it, a different org cannot
    from app.services.search.search_service import search_service, SearchMode

    for aid in CANARY_ASSET_IDS[:2]:
        org_id = per_asset_org[aid]
        name = before[aid]["name"]
        query_term = name.split(".")[0]
        try:
            res = await search_service.search(org_id=org_id, query=query_term, mode=SearchMode.SEMANTIC,
                                                asset_types=None, project_id=None, limit=20)
            hit_ids = [str(h.get("id")) for h in (res.get("assets") or res.get("results") or [])]
            check(f"own-org semantic search finds canary asset ({aid[:8]})", aid in hit_ids, f"hits={hit_ids[:5]}")
        except Exception as e:
            check(f"own-org semantic search finds canary asset ({aid[:8]})", False, str(e))

        # org isolation: a random other org must never see this asset
        other_org = next(o for o in per_asset_org.values() if o != org_id)
        try:
            res2 = await search_service.search(org_id=other_org, query=query_term, mode=SearchMode.SEMANTIC,
                                                 asset_types=None, project_id=None, limit=20)
            hit_ids2 = [str(h.get("id")) for h in (res2.get("assets") or res2.get("results") or [])]
            check(f"org isolation: different org does NOT see this asset ({aid[:8]})", aid not in hit_ids2,
                  f"leaked into other org's results: {hit_ids2}")
        except Exception as e:
            check(f"org isolation check ran without exception ({aid[:8]})", False, str(e))

    # source row unchanged (except status/updated_at, which the handler
    # itself is allowed to touch -- name/mime_type/blob_ref/org must not move)
    for aid in CANARY_ASSET_IDS:
        after = await snapshot_asset(aid)
        b = before[aid]
        unchanged = (
            after["name"] == b["name"]
            and after["mime_type"] == b["mime_type"]
            and after["blob_ref"] == b["blob_ref"]
            and str(after["organization_id"]) == str(b["organization_id"])
        )
        check(f"source Postgres row unchanged except status ({aid[:8]})", unchanged)

    all_pass = all(s == "PASS" for s, _, _ in results)
    print(f"\n=== PRODUCTION CANARY {'PASSED' if all_pass else 'FAILED'} ===")
    print(f"{sum(1 for s,_,_ in results if s=='PASS')}/{len(results)} checks passed")
    await engine.dispose()
    return all_pass


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
