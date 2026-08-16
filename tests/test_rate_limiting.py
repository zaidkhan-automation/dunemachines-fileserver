"""
Finding #5: RATE_LIMIT_SEARCH/RATE_LIMIT_UPLOAD existed in config but
were never wired into any enforcement anywhere in the service. This adds
slowapi limits to /uploads/init (10/min), /uploads/{id}/complete
(5/min), /uploads/{id}/download-url (50/min).

Uses TestClient(app) without the `with` context manager, same trick
used for the sibling Omnius CORS test — this avoids triggering the
FastAPI lifespan's on_startup() (DB/Qdrant/Redis connections), which
isn't needed just to exercise routing/middleware behavior.

require_permission("assets", "upload") builds a fresh closure per call,
so it can't be targeted directly via app.dependency_overrides (keyed by
object identity). Overriding the shared get_current_user dependency +
patching check_permission to always allow sidesteps that without
needing to reach into the route's actual dependency object.
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app
from app.core.security import get_current_user
from app.core.database import get_db


async def _fake_user():
    return {"user_id": "u1", "org_id": "org-1", "roles": ["editor"]}


def test_uploads_init_rate_limited_after_10_per_minute():
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    try:
        with patch("app.services.permissions.rbac.check_permission", return_value=True), \
             patch("app.api.rest.uploads.upload_service.init_upload", AsyncMock(side_effect=Exception("stop before DB"))):
            statuses = []
            for _ in range(12):
                r = client.post(
                    "/api/v1/uploads/init",
                    json={"filename": "a.txt", "mime_type": "text/plain", "size_bytes": 10},
                )
                statuses.append(r.status_code)

        assert 429 in statuses, f"expected a 429 among {statuses} after exceeding 10/min"
    finally:
        app.dependency_overrides.clear()


def test_download_url_rate_limit_allows_more_than_upload_init():
    """Sanity check the limits are actually different per endpoint, not
    one shared global bucket — download-url (50/min) must not 429 at the
    same low request count that trips upload/init (10/min)."""
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    try:
        with patch("app.repositories.asset_repo.asset_repo.get_by_id", AsyncMock(return_value=None)):
            statuses = []
            for _ in range(12):
                r = client.get("/api/v1/uploads/some-fake-id/download-url")
                statuses.append(r.status_code)

        assert 429 not in statuses, f"download-url shouldn't 429 within 12 requests (limit is 50/min): {statuses}"
    finally:
        app.dependency_overrides.clear()
