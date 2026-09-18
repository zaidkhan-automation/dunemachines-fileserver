"""Tests for the F-02 remediation (production audit, 2026-09-18): upload
completion no longer trusts a client-supplied object_key.

VULNERABILITY (confirmed via direct code reading before this fix — not
reproduced by reverting production code, since that would require either
live-editing the running service's files or complex git-stash gymnastics
on files that already carry unrelated pre-existing uncommitted work; the
code-level evidence below is exact and sufficient):

  Before this fix, app/services/uploads/upload_service.py's
  complete_upload(asset_id, object_key, checksum) took `object_key`
  straight from the client's request body (app/api/rest/uploads.py's
  CompleteUploadRequest.object_key), called only
  _verify_object_exists(object_key) (an org-UNSCOPED S3 existence check),
  and the REST handler then wrote req.object_key verbatim into
  asset_repo.update_blob(..., blob_ref=req.object_key, ...) — for
  `upload_id`, an asset the route had already confirmed belongs to the
  CALLER's own org (asset_repo.get_by_id(db, upload_id, _org)), but with
  NO check that object_key itself belongs to that same org/asset. Object
  keys are not secret — they appear in every presigned download/
  thumbnail URL. A caller who owns any asset in any org, holding another
  org's real object_key, could call /complete on their OWN asset with
  the FOREIGN key and bind their own (ownership-check-passing) asset to
  that foreign org's actual file content.

FIX: complete_upload now takes (asset_id, org_id, filename) — all three
already sourced from the caller's own org-scoped Asset row — and always
RE-DERIVES the object_key via the same _get_object_key(org_id, asset_id,
filename) function init_upload used to mint the original presigned PUT
URL. The client's object_key field is accepted (for backward
compatibility with existing clients that still send it) but is now
completely inert. Cross-org/cross-asset binding is structurally
impossible after this fix, not just checked-and-rejected.

Two layers of tests, matching this repo's own established split (see
test_presentation_links_router.py/test_presentation_links_service.py for
the same pattern applied elsewhere): service-layer tests use real DB +
real MinIO (conftest.py's autouse S3 lifecycle fixture) to prove the
actual security property; route-layer tests use TestClient with DB/S3
mocked (TestClient's internal event-loop portal doesn't mix safely with a
real async DB/S3 connection created outside it — confirmed directly while
writing this file, and already the documented reason
test_presentation_links_router.py mocks instead of using a real DB) to
prove the route passes the right arguments to the service.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from main import app
from app.core.database import AsyncSessionLocal, get_db
from app.core.security import get_current_user
from app.core.s3_client import get_s3_client
from app.core.config import settings
from app.repositories.asset_repo import asset_repo
from app.models.asset import Asset, AssetStatus
from app.services.uploads.upload_service import upload_service

# Applied per-function (not module-wide via pytestmark) since the two
# route-layer tests below are deliberately plain sync functions — see
# their own docstrings for why.
_async_session = pytest.mark.asyncio(loop_scope="session")


async def _make_pending_asset(db, org_id, filename="test.txt"):
    """Mirrors what POST /uploads/init actually persists: a PENDING asset
    with no blob_ref yet."""
    asset = await asset_repo.create(db, {
        "organization_id": org_id,
        "created_by": str(uuid.uuid4()),
        "name": filename,
        "asset_type": "document",
        "status": AssetStatus.PENDING,
    })
    await db.commit()
    return asset


async def _put_real_object(org_id, asset_id, filename, content: bytes) -> str:
    """Puts content at the exact server-authoritative key for
    (org_id, asset_id, filename) — simulating a real client PUT against
    the presigned URL init_upload would have issued."""
    object_key = upload_service._get_object_key(org_id, asset_id, filename)
    s3 = get_s3_client()
    await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=object_key, Body=content)
    return object_key


async def _cleanup(db, asset_id, object_keys=()):
    s3 = get_s3_client()
    for key in object_keys:
        try:
            await s3.delete_object(Bucket=settings.STORAGE_BUCKET, Key=key)
        except Exception:
            pass
    await db.execute(delete(Asset).where(Asset.id == asset_id))
    await db.commit()


# ---------------------------------------------------------------------------
# Service-layer tests (no HTTP, no rate-limit exposure) — cover the
# invariant directly at the boundary that was actually vulnerable.
# ---------------------------------------------------------------------------

@_async_session
async def test_complete_upload_derives_key_from_asset_identity_not_caller_input():
    """The new signature has no object_key parameter to smuggle a foreign
    key through in the first place — this is the core structural proof."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await _make_pending_asset(db, org_id)
        real_key = await _put_real_object(org_id, str(asset.id), asset.name, b"legit content")
        try:
            result = await upload_service.complete_upload(str(asset.id), org_id, asset.name)
            assert result["object_key"] == real_key
        finally:
            await _cleanup(db, asset.id, [real_key])


@_async_session
async def test_complete_upload_cross_org_asset_cannot_bind_to_foreign_content():
    """Direct reproduction of the vulnerability's core scenario at the
    service layer: even if a caller somehow supplied org A's real
    filename/asset pairing, completion for org B's OWN asset can only
    ever derive an org-B-prefixed key — org A's actual object is never
    reachable through it."""
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset_a = await _make_pending_asset(db, org_a, filename="shared_name.txt")
        asset_b = await _make_pending_asset(db, org_b, filename="shared_name.txt")
        key_a = await _put_real_object(org_a, str(asset_a.id), asset_a.name, b"org A secret content")
        try:
            # Org B completing ITS OWN asset — even though org A has an
            # object at the exact same filename, the derived key is keyed
            # by (org_b, asset_b.id, filename), never (org_a, asset_a.id, ...).
            with pytest.raises(ValueError):
                # Org B's own object was never actually uploaded — this
                # MUST fail with "not found", proving the derived key
                # points at org B's own (empty) slot, not org A's real one.
                await upload_service.complete_upload(str(asset_b.id), org_b, asset_b.name)

            derived_key_b = upload_service._get_object_key(org_b, str(asset_b.id), asset_b.name)
            assert derived_key_b != key_a
            assert org_a not in derived_key_b
        finally:
            await _cleanup(db, asset_a.id, [key_a])
            await _cleanup(db, asset_b.id, [])


@_async_session
async def test_complete_upload_nonexistent_object_rejected():
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await _make_pending_asset(db, org_id)
        try:
            with pytest.raises(ValueError, match="Object not found in storage"):
                await upload_service.complete_upload(str(asset.id), org_id, asset.name)
        finally:
            await _cleanup(db, asset.id, [])


@_async_session
async def test_complete_upload_replay_is_idempotent_not_destructive():
    """Calling complete_upload twice for the same, already-uploaded asset
    must not fail or corrupt anything — it just re-derives and
    re-verifies the same real key both times."""
    org_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await _make_pending_asset(db, org_id)
        real_key = await _put_real_object(org_id, str(asset.id), asset.name, b"content")
        try:
            first = await upload_service.complete_upload(str(asset.id), org_id, asset.name)
            second = await upload_service.complete_upload(str(asset.id), org_id, asset.name)
            assert first["object_key"] == second["object_key"] == real_key
        finally:
            await _cleanup(db, asset.id, [real_key])


# ---------------------------------------------------------------------------
# Route-level tests (real HTTP via TestClient) — prove the actual wiring:
# the route passes asset.organization_id/asset.name, not req.object_key.
# Kept to 2 calls total against /complete (5/minute limit, shared Redis
# counter, no per-test reset in this suite's existing convention).
# ---------------------------------------------------------------------------

def _override_user(org_id: str, user_id: str = None):
    async def _fake():
        return {"user_id": user_id or str(uuid.uuid4()), "org_id": org_id, "roles": ["editor"]}
    return _fake


def test_route_calls_service_with_asset_identity_never_client_object_key():
    """Wiring proof: the route must call upload_service.complete_upload
    with (upload_id, asset.organization_id, asset.name) — the already
    org-scoped Asset row's own fields — and must NEVER pass through
    req.object_key. DB and S3 mocked (matching
    tests/test_rate_limiting.py's and
    tests/test_presentation_links_router.py's own established convention
    for TestClient router tests — this repo deliberately doesn't mix
    TestClient's internal event-loop portal with a real DB/S3 connection;
    that boundary is exercised for real by the service-layer tests above
    instead, matching the split test_presentation_links_router.py/
    test_presentation_links_service.py already uses for the same reason)."""
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    fake_asset = SimpleNamespace(
        id=asset_id, organization_id=org_id, name="real_asset_name.txt", blob_ref=None,
        mime_type="text/plain", extra_data={},  # F-07 (remediation audit): route now reads these too
    )

    client = TestClient(app)
    app.dependency_overrides[get_current_user] = _override_user(org_id)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch("app.api.rest.uploads.asset_repo.get_by_id", AsyncMock(return_value=fake_asset)), \
             patch("app.api.rest.uploads.asset_repo.update_blob", AsyncMock()), \
             patch("app.api.rest.uploads.publish_upload_complete", AsyncMock()), \
             patch(
                 "app.api.rest.uploads.upload_service.complete_upload",
                 AsyncMock(return_value={"asset_id": asset_id, "object_key": "orgs/.../real_asset_name.txt", "status": "processing", "message": "ok"}),
             ) as mock_complete:
            resp = client.post(
                f"/api/v1/uploads/{asset_id}/complete",
                json={"object_key": "attacker/supplied/nonsense/key", "checksum": None},
            )
            assert resp.status_code == 200, resp.text
            mock_complete.assert_awaited_once()
            call_args = mock_complete.await_args.args
            assert call_args[0] == asset_id
            assert call_args[1] == org_id  # asset.organization_id, not anything client-supplied
            assert call_args[2] == "real_asset_name.txt"  # asset.name, not req.object_key
            assert "attacker" not in str(call_args)
    finally:
        app.dependency_overrides.clear()


def test_route_rejects_when_service_reports_object_not_found():
    """The 422 path (service.complete_upload raising ValueError, e.g.
    because the caller's own org/asset has no real object at the
    server-derived key) still surfaces correctly through the route."""
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    fake_asset = SimpleNamespace(
        id=asset_id, organization_id=org_id, name="nope.txt", blob_ref=None,
        mime_type="text/plain", extra_data={},  # F-07 (remediation audit): route now reads these too
    )

    client = TestClient(app)
    app.dependency_overrides[get_current_user] = _override_user(org_id)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch("app.api.rest.uploads.asset_repo.get_by_id", AsyncMock(return_value=fake_asset)), \
             patch(
                 "app.api.rest.uploads.upload_service.complete_upload",
                 AsyncMock(side_effect=ValueError("Object not found in storage: orgs/.../nope.txt")),
             ):
            resp = client.post(
                f"/api/v1/uploads/{asset_id}/complete",
                json={"object_key": "irrelevant", "checksum": None},
            )
            assert resp.status_code == 422, resp.text
    finally:
        app.dependency_overrides.clear()
