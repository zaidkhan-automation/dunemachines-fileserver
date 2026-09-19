"""Read-only deterministic reconciliation of Postgres 'ready' assets
against actual Qdrant points, by point ID -- not aggregate counts.

For every ready, non-deleted asset:
  - point_id = str(uuid.UUID(asset_id))        (indexer._point_id)
  - collection = f"fileserver_assets_{org_id[:8]}"  (indexer._collection_name)
  - classify:
      EXISTING_POINT         -- point already present in Qdrant
      MISSING_POINT          -- point absent, asset otherwise indexable
      SOURCE_OBJECT_MISSING  -- blob_ref is NULL, or the MinIO object it
                                 points at doesn't exist (flagged, not
                                 excluded -- production's own text-extract
                                 fallback to filename still indexes these)
      NOT_INDEXABLE          -- no usable text source at all (no blob_ref
                                 AND no name -- should not exist given DB
                                 constraints, checked anyway)
      INDEX_ERROR            -- asset_id/org_id not valid UUIDs, or some
                                 other structural problem

Writes NOTHING. Only reads Postgres and Qdrant.

Run: venv/bin/python3 scripts/utilities/qdrant_reindex_reconcile.py [--json out.json]
"""
import asyncio
import json
import sys
import uuid
from collections import defaultdict

sys.path.insert(0, ".")

from app.core.database import engine
from app.core.s3_client import get_s3_client, init_s3_client
from app.core.config import settings
from app.services.search.indexer import get_qdrant, _collection_name, _point_id
from sqlalchemy import text


async def fetch_ready_assets():
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT id, organization_id, name, asset_type, source_type, mime_type, blob_ref, blob_bucket
            FROM assets
            WHERE deleted_at IS NULL AND status = 'ready'
        """))
        return [dict(row._mapping) for row in result]


async def object_exists(object_key: str) -> bool:
    try:
        s3 = get_s3_client()
        await s3.head_object(Bucket=settings.STORAGE_BUCKET, Key=object_key)
        return True
    except Exception:
        return False


async def main():
    await init_s3_client()

    all_assets = await fetch_ready_assets()
    print(f"total ready, non-deleted assets: {len(all_assets)}")

    # Structural NOT_INDEXABLE, decided BEFORE touching Qdrant or S3 at all:
    #   - asset_type == 'folder': no content by definition.
    #   - blob_ref IS NULL: no MinIO object was ever created for this asset
    #     (confirmed live: every current NULL-blob_ref ready asset is either
    #     a folder, or source_type='github' -- github-sourced items have no
    #     MinIO blob at all; the production handle_upload_complete handler
    #     itself requires a truthy object_key and returns immediately
    #     without one, so these are not indexable via the existing pipeline
    #     as it stands today, not a data bug).
    not_indexable = []
    assets = []
    for a in all_assets:
        if a["asset_type"] == "folder":
            not_indexable.append({"asset_id": str(a["id"]), "org_id": str(a["organization_id"]),
                                   "reason": "asset_type=folder, no content"})
        elif not a["blob_ref"]:
            not_indexable.append({"asset_id": str(a["id"]), "org_id": str(a["organization_id"]),
                                   "reason": f"blob_ref is NULL (source_type={a.get('source_type')}) -- "
                                             f"handle_upload_complete requires a truthy object_key"})
        else:
            assets.append(a)
    print(f"structurally NOT_INDEXABLE (folder / no blob_ref): {len(not_indexable)}")
    print(f"remaining indexable-population candidates: {len(assets)}")

    # Classify structural validity + group by target collection for
    # batched Qdrant retrieval (one retrieve() call per collection instead
    # of one per asset).
    by_collection = defaultdict(list)  # collection -> [(asset, point_id)]
    index_error = []
    for a in assets:
        try:
            point_id = _point_id(str(a["id"]))
            collection = _collection_name(str(a["organization_id"]))
            uuid.UUID(str(a["organization_id"]))  # validate org_id shape too
        except Exception as e:
            index_error.append({"asset_id": str(a["id"]), "reason": str(e)})
            continue
        by_collection[collection].append((a, point_id))

    qc = get_qdrant()
    existing_collections = {c.name for c in (await qc.get_collections()).collections}

    existing_points = []
    missing_points = []

    for collection, entries in by_collection.items():
        if collection not in existing_collections:
            # Whole collection doesn't exist yet -> every point in it is missing.
            missing_points.extend(entries)
            continue
        ids = [pid for _, pid in entries]
        # Qdrant retrieve() in reasonably sized chunks.
        found_ids = set()
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            pts = await qc.retrieve(collection_name=collection, ids=chunk)
            found_ids.update(p.id for p in pts)
        for asset, pid in entries:
            if pid in found_ids:
                existing_points.append((asset, pid))
            else:
                missing_points.append((asset, pid))

    print(f"existing points: {len(existing_points)}")
    print(f"missing points: {len(missing_points)}")
    print(f"index_error (structural): {len(index_error)}")

    # Source-object check for missing candidates only (existing points
    # already proved they were indexable once; no need to re-check them).
    # Every entry reaching this loop already has a non-NULL blob_ref
    # (pre-filtered above), so this is checking "did the object this row
    # points at actually survive" (e.g. deleted out-of-band in storage),
    # not "was one ever assigned."
    source_missing = []
    truly_missing = []
    print("\nchecking backing object existence for missing candidates (MinIO HEAD)...")
    for i, (asset, pid) in enumerate(missing_points):
        blob_ref = asset["blob_ref"]
        exists = await object_exists(blob_ref)
        if not exists:
            source_missing.append({"asset_id": str(asset["id"]), "org_id": str(asset["organization_id"]),
                                    "reason": f"object not found: {blob_ref}"})
        else:
            truly_missing.append(asset)
        if (i + 1) % 200 == 0:
            print(f"  checked {i + 1}/{len(missing_points)}")

    print(f"\nsource_missing (blob_ref set but object gone from storage): {len(source_missing)}")
    print(f"repair candidates (missing point + source object confirmed present): {len(truly_missing)}")

    # Breakdown by org and mime type for the repair candidate set.
    by_org = defaultdict(int)
    by_mime = defaultdict(int)
    for a in truly_missing:
        by_org[str(a["organization_id"])] += 1
        by_mime[a.get("mime_type") or "unknown"] += 1

    summary = {
        "total_ready_assets": len(all_assets),
        "structurally_not_indexable_count": len(not_indexable),
        "structurally_not_indexable": not_indexable,
        "indexable_population": len(assets),
        "existing_points": len(existing_points),
        "missing_points_total": len(missing_points),
        "index_error": index_error,
        "source_missing": source_missing,
        "repair_candidates_count": len(truly_missing),
        "repair_candidates_by_org": dict(sorted(by_org.items(), key=lambda x: -x[1])),
        "repair_candidates_by_mime": dict(sorted(by_mime.items(), key=lambda x: -x[1])),
        "repair_candidate_asset_ids": [str(a["id"]) for a in truly_missing],
        "repair_candidate_org_ids": {str(a["id"]): str(a["organization_id"]) for a in truly_missing},
    }

    if "--json" in sys.argv:
        out_path = sys.argv[sys.argv.index("--json") + 1]
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"\nwrote {out_path}")

    await engine.dispose()
    return summary


if __name__ == "__main__":
    asyncio.run(main())
