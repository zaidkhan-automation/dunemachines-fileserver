"""Tests for the F-07 (content-type sniff/serve safety) and F-11
(streaming response) remediation (production audit, 2026-09-18).

F-07: settings.ALLOWED_MIME_TYPES stays [] (this product intentionally
accepts arbitrary file types — a restrictive allowlist would break real
usage). Instead, upload_service._sniff_content_type flags a
declared-vs-detected mismatch or a detected dangerous-if-rendered type
(HTML/SVG/JS) into the asset's extra_data at completion time, and
files.py::get_asset_content enforces safety AT SERVE TIME regardless of
what's declared: X-Content-Type-Options: nosniff always, and
Content-Disposition: attachment forced for anything in
DANGEROUS_RENDER_MIME_TYPES or sniff-flagged dangerous. Uploads are never
blocked by any of this.

F-11: get_asset_content now returns a StreamingResponse (chunked reads)
instead of buffering the whole object into memory with a single .read().

Real DB + real MinIO (conftest.py's autouse S3 lifecycle fixture),
matching this repo's established convention.
"""
import os
import uuid

import pytest
from fastapi.responses import StreamingResponse
from sqlalchemy import delete

from app.core.database import AsyncSessionLocal
from app.core.s3_client import get_s3_client
from app.core.config import settings
from app.repositories.asset_repo import asset_repo
from app.models.asset import Asset, AssetStatus
from app.api.rest.files import get_asset_content
from app.services.uploads.upload_service import upload_service, DANGEROUS_RENDER_MIME_TYPES

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_asset(db, org_id, content: bytes, mime_type="text/plain", extra_data=None):
    blob_ref = f"test-content-type-safety/{uuid.uuid4()}.bin"
    s3 = get_s3_client()
    await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=blob_ref, Body=content)
    asset = await asset_repo.create(db, {
        "organization_id": org_id,
        "created_by": str(uuid.uuid4()),
        "name": "test.bin",
        "asset_type": "document",
        "status": AssetStatus.READY,
        "blob_ref": blob_ref,
        "size_bytes": len(content),
        "mime_type": mime_type,
        "extra_data": extra_data or {},
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


# ---------------------------------------------------------------------------
# F-07: upload-time sniff
# ---------------------------------------------------------------------------

async def test_sniff_flags_html_masquerading_as_declared_text_plain():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await asset_repo.create(db, {
            "organization_id": org_id, "created_by": str(uuid.uuid4()),
            "name": "innocent.txt", "asset_type": "document",
            "status": AssetStatus.PENDING, "mime_type": "text/plain",
        })
        await db.commit()
        object_key = upload_service._get_object_key(org_id, str(asset.id), asset.name)
        s3 = get_s3_client()
        await s3.put_object(
            Bucket=settings.STORAGE_BUCKET, Key=object_key,
            Body=b"<html><body><script>alert(document.cookie)</script></body></html>",
        )
        try:
            result = await upload_service.complete_upload(
                str(asset.id), org_id, asset.name, declared_mime_type="text/plain",
            )
            sniff = result["content_sniff"]
            assert sniff is not None
            assert sniff["declared"] == "text/plain"
            assert sniff["detected"] == "text/html"
            assert sniff["matched"] is False
            assert sniff["dangerous"] is True
        finally:
            await _cleanup(db, asset.id, object_key)


async def test_sniff_returns_none_for_ordinary_matching_upload():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await asset_repo.create(db, {
            "organization_id": org_id, "created_by": str(uuid.uuid4()),
            "name": "notes.txt", "asset_type": "document",
            "status": AssetStatus.PENDING, "mime_type": "text/plain",
        })
        await db.commit()
        object_key = upload_service._get_object_key(org_id, str(asset.id), asset.name)
        s3 = get_s3_client()
        await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=object_key, Body=b"just some plain notes, nothing weird")
        try:
            result = await upload_service.complete_upload(
                str(asset.id), org_id, asset.name, declared_mime_type="text/plain",
            )
            assert result["content_sniff"] is None
        finally:
            await _cleanup(db, asset.id, object_key)


async def test_sniff_never_blocks_completion_even_when_dangerous():
    """Uploads are never rejected by this — a real SVG (a completely
    legitimate, commonly-uploaded file type) declared honestly still
    completes successfully; it's just flagged, and safety is enforced at
    serve time instead."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await asset_repo.create(db, {
            "organization_id": org_id, "created_by": str(uuid.uuid4()),
            "name": "icon.svg", "asset_type": "document",
            "status": AssetStatus.PENDING, "mime_type": "image/svg+xml",
        })
        await db.commit()
        object_key = upload_service._get_object_key(org_id, str(asset.id), asset.name)
        s3 = get_s3_client()
        await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=object_key, Body=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        try:
            result = await upload_service.complete_upload(
                str(asset.id), org_id, asset.name, declared_mime_type="image/svg+xml",
            )
            assert result["status"] == AssetStatus.PROCESSING
            assert result["content_sniff"]["dangerous"] is True
            assert result["content_sniff"]["matched"] is True  # honestly declared, still flagged as dangerous
        finally:
            await _cleanup(db, asset.id, object_key)


async def test_sniff_failure_never_raises_and_upload_still_completes():
    """A libmagic/import problem must degrade to 'no sniff data', never
    fail the upload."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await asset_repo.create(db, {
            "organization_id": org_id, "created_by": str(uuid.uuid4()),
            "name": "x.bin", "asset_type": "document",
            "status": AssetStatus.PENDING, "mime_type": "application/octet-stream",
        })
        await db.commit()
        object_key = upload_service._get_object_key(org_id, str(asset.id), asset.name)
        s3 = get_s3_client()
        await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=object_key, Body=b"\x00\x01\x02")
        import unittest.mock as mock
        try:
            with mock.patch("magic.from_buffer", side_effect=RuntimeError("libmagic unavailable")):
                result = await upload_service.complete_upload(
                    str(asset.id), org_id, asset.name, declared_mime_type="application/octet-stream",
                )
            assert result["status"] == AssetStatus.PROCESSING
            assert result["content_sniff"] is None
        finally:
            await _cleanup(db, asset.id, object_key)


# ---------------------------------------------------------------------------
# F-07: serve-time safety
# ---------------------------------------------------------------------------

async def test_get_asset_content_always_sets_nosniff():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, b"ordinary content", mime_type="text/plain")
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            assert resp.headers.get("x-content-type-options") == "nosniff"
            assert "content-disposition" not in {k.lower() for k in resp.headers.keys()}
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_get_asset_content_forces_attachment_for_declared_dangerous_mime():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(
            db, org_id, b"<html><script>alert(1)</script></html>", mime_type="text/html",
        )
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            assert "attachment" in resp.headers.get("content-disposition", "")
            assert resp.headers.get("x-content-type-options") == "nosniff"
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_get_asset_content_forces_attachment_when_sniff_flagged_dangerous():
    """Even a file declared as something SAFE gets forced to attachment
    at serve time if upload-time sniffing flagged it dangerous — the
    declared type alone is never trusted for this decision."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(
            db, org_id, b"<html><script>alert(1)</script></html>", mime_type="text/plain",
            extra_data={"content_sniff": {"declared": "text/plain", "detected": "text/html", "matched": False, "dangerous": True}},
        )
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            assert "attachment" in resp.headers.get("content-disposition", "")
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_get_asset_content_does_not_force_attachment_for_safe_types():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, b"PNGDATA", mime_type="image/png")
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            assert "content-disposition" not in {k.lower() for k in resp.headers.keys()}
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_arbitrary_unusual_file_type_still_served_not_rejected():
    """Proves F-07 never became a restrictive allowlist — an unusual,
    legitimate content type round-trips exactly as before."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, b"\x50\x4b\x03\x04fake zip bytes", mime_type="application/x-blender")
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            chunks = [c async for c in resp.body_iterator]
            assert b"".join(chunks) == b"\x50\x4b\x03\x04fake zip bytes"
            assert resp.media_type == "application/x-blender"
        finally:
            await _cleanup(db, asset.id, blob_ref)


# ---------------------------------------------------------------------------
# F-11: streaming
# ---------------------------------------------------------------------------

async def test_get_asset_content_is_a_streaming_response():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, b"small")
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            assert isinstance(resp, StreamingResponse)
        finally:
            await _cleanup(db, asset.id, blob_ref)


async def test_get_asset_content_streams_multi_mb_object_byte_identical():
    """A few MB is enough to meaningfully exercise chunked reads (the
    endpoint's internal chunk size is far smaller than this) without
    generating anything close to production-scale load."""
    org_id = str(uuid.uuid4())
    payload = os.urandom(3 * 1024 * 1024)  # 3 MB
    async with AsyncSessionLocal() as db:
        asset, blob_ref = await _make_asset(db, org_id, payload, mime_type="application/octet-stream")
        try:
            resp = await get_asset_content(str(asset.id), db=db, user={"org_id": org_id})
            chunk_count = 0
            collected = bytearray()
            async for chunk in resp.body_iterator:
                chunk_count += 1
                collected.extend(chunk)
            assert bytes(collected) == payload
            assert chunk_count > 1, "expected multiple chunks for a 3MB object, got a single read"
        finally:
            await _cleanup(db, asset.id, blob_ref)
