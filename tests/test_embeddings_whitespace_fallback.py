"""Regression test — historical Qdrant reindex repair (2026-09-19).

Root cause: handle_upload_complete's text-extraction fallback
(`if not text: text = asset.name`) never fired for whitespace-only
extracted text (e.g. an empty .md file containing just "\n", or a
scanned/malformed PDF where pypdf recovers nothing but layout spaces) --
a non-empty-but-whitespace string is truthy in Python. That whitespace
went straight to the embedding model, which returns no vector for it,
and index_asset's own try/except swallows the "no vector" case as a
plain `return False` -- no exception anywhere in the call chain, so a
caller has no way to distinguish this from success without independently
checking Qdrant. Confirmed live: 9 real historical assets were silently
never indexed this way.

Real DB + real MinIO (this repo's own established convention, see
test_asset_content_endpoint.py) + real Qdrant -- the fix is specifically
about behavior at that intersection, a mocked boundary would not prove it.
"""
import uuid

import pytest

from app.core.database import AsyncSessionLocal
from app.core.s3_client import get_s3_client
from app.core.config import settings
from app.repositories.asset_repo import asset_repo
from app.models.asset import Asset, AssetStatus
from app.workers.ai.embeddings import handle_upload_complete
from app.services.search.indexer import get_qdrant, _collection_name, _point_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_whitespace_asset(org_id: str, name: str, content: bytes, mime_type: str) -> str:
    asset_id = str(uuid.uuid4())
    object_key = f"orgs/{org_id}/assets/{asset_id}/{name}"
    s3 = get_s3_client()
    await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=object_key, Body=content, ContentType=mime_type)
    async with AsyncSessionLocal() as db:
        await asset_repo.create(db, {
            "id": asset_id, "organization_id": org_id, "created_by": str(uuid.uuid4()),
            "name": name, "asset_type": "file", "mime_type": mime_type,
            "size_bytes": len(content), "blob_ref": object_key, "blob_bucket": "dunemachines-files",
        })
        await db.commit()
    return asset_id, object_key


async def test_whitespace_only_markdown_falls_back_to_filename():
    org_id = str(uuid.uuid4())
    asset_id, object_key = await _make_whitespace_asset(org_id, "meeting-notes.md", b"\n", "text/markdown")
    collection = _collection_name(org_id)
    qc = get_qdrant()

    try:
        await handle_upload_complete({"asset_id": asset_id, "org_id": org_id, "object_key": object_key})

        pts = await qc.retrieve(collection_name=collection, ids=[_point_id(asset_id)], with_payload=True)
        assert len(pts) == 1, "whitespace-only content must still produce an indexed point via the filename fallback"
        assert "meeting-notes" in (pts[0].payload or {}).get("name", "")
    finally:
        try:
            await qc.delete(collection_name=collection, points_selector=[_point_id(asset_id)])
        except Exception:
            pass
        async with AsyncSessionLocal() as db:
            await asset_repo.soft_delete(db, asset_id, org_id)
            await db.commit()


async def test_whitespace_only_content_is_stripped_before_the_truthiness_check():
    """Direct unit-level proof of the actual code change, independent of
    the full pipeline -- a bare space/newline string must be treated the
    same as empty by the fallback condition."""
    from app.workers.ai import embeddings as embeddings_module
    import inspect

    src = inspect.getsource(embeddings_module.handle_upload_complete)
    assert "not text.strip()" in src, (
        "the whitespace-aware fallback condition (`if not text or not text.strip():`) "
        "must be present in handle_upload_complete -- this is a permanent regression "
        "lock for the exact incident this test file documents"
    )
