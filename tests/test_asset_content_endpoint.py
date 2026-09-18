"""
Tests for GET /assets/{asset_id}/content — added for the Org Files
Unification cloud->local hydration path (dunemachines_backend's
hydrate_user_from_fileserver), which needs to download ANY asset in the
caller's org, not just ones reachable through omnius_files.py's
per-user-subfolder /read bridge.

Real DB + real MinIO (conftest.py's autouse S3 lifecycle fixture already
handles init/close), matching this repo's own established convention —
no mocked HTTP.
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal
from app.core.s3_client import get_s3_client
from app.core.config import settings
from app.repositories.asset_repo import asset_repo
from app.models.asset import Asset, AssetStatus
from app.api.rest.files import get_asset_content

# conftest.py's S3 client lifecycle fixture is session-scoped
# (loop_scope="session") — these tests touch that same client, so they
# need to run on that same event loop rather than pytest-asyncio's
# per-test default, or aioboto3's connector (bound to whichever loop
# created it) raises across the loop boundary.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_asset(db, org_id, content: bytes, mime_type="text/plain", status=AssetStatus.READY):
    blob_ref = f"test-asset-content/{uuid.uuid4()}.bin"
    s3 = get_s3_client()
    await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=blob_ref, Body=content)
    asset = await asset_repo.create(db, {
        "organization_id": org_id,
        "created_by": str(uuid.uuid4()),
        "name": "test.txt",
        "asset_type": "document",
        "status": status,
        "blob_ref": blob_ref,
        "size_bytes": len(content),
        "mime_type": mime_type,
    })
    await db.commit()
    return asset, blob_ref


async def _cleanup(db, asset_id, blob_ref=None):
    if blob_ref:
        s3 = get_s3_client()
        try:
            await s3.delete_object(Bucket=settings.STORAGE_BUCKET, Key=blob_ref)
        except Exception:
            pass
    await db.execute(delete(Asset).where(Asset.id == asset_id))
    await db.commit()


async def test_get_asset_content_returns_real_bytes():
    """F-11 (remediation audit) switched this endpoint from a buffered
    Response to a StreamingResponse — resp.body no longer exists, so this
    test now drains resp.body_iterator to reconstruct the full payload
    and assert it's byte-identical, exercising the real chunked read path
    rather than a single .read()."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, b"hello hydration")
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            chunks = [chunk async for chunk in resp.body_iterator]
            assert b"".join(chunks) == b"hello hydration"
            assert resp.media_type == "text/plain"
            assert resp.headers.get("x-content-type-options") == "nosniff"
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_get_asset_content_404_for_wrong_org():
    """The exact isolation property hydration depends on: an asset from
    one org must never be fetchable with another org's identity."""
    org_id = str(uuid.uuid4())
    other_org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, b"secret bytes")
        try:
            with pytest.raises(HTTPException) as exc_info:
                await get_asset_content(str(asset.id), db=db, user={"org_id": other_org_id})
            assert exc_info.value.status_code == 404
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_get_asset_content_rejects_folder():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        folder = await asset_repo.create(db, {
            "organization_id": org_id,
            "created_by": str(uuid.uuid4()),
            "name": "a_folder",
            "asset_type": "folder",
            "status": AssetStatus.READY,
        })
        await db.commit()
        try:
            with pytest.raises(HTTPException) as exc_info:
                await get_asset_content(str(folder.id), db=db, user={"org_id": org_id})
            assert exc_info.value.status_code == 400
        finally:
            await _cleanup(db, folder.id)


async def test_get_asset_content_404_when_no_blob_ref():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await asset_repo.create(db, {
            "organization_id": org_id,
            "created_by": str(uuid.uuid4()),
            "name": "pending.txt",
            "asset_type": "document",
            "status": AssetStatus.PENDING,
        })
        await db.commit()
        try:
            with pytest.raises(HTTPException) as exc_info:
                await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            assert exc_info.value.status_code == 404
        finally:
            await _cleanup(db, asset.id)
