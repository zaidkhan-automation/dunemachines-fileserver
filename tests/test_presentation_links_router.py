"""
Router-level tests for app/api/rest/presentation_links.py — auth wiring,
public-no-auth wiring, rate limiting, and the uniform-404 data-leak
boundary. Same TestClient(app)-without-lifespan + dependency_overrides
+ patch convention as tests/test_rate_limiting.py and
tests/test_upload_input_validation.py; service-layer correctness is
covered separately in tests/test_presentation_links_service.py.
"""
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from fastapi.testclient import TestClient

from main import app
from app.core.security import get_current_user
from app.core.database import get_db
from app.services.presentation_service import ResolveResult


async def _fake_user():
    return {"user_id": "11111111-1111-1111-1111-111111111111", "org_id": "org-1", "roles": ["editor"]}


def test_create_link_requires_auth():
    client = TestClient(app)
    r = client.post("/api/v1/files/some-file-id/presentation-links", json={"mode": "live"})
    assert r.status_code in (401, 403)


def test_list_links_requires_auth():
    client = TestClient(app)
    r = client.get("/api/v1/files/some-file-id/presentation-links")
    assert r.status_code in (401, 403)


def test_revoke_link_requires_auth():
    client = TestClient(app)
    r = client.delete("/api/v1/presentation-links/some-id")
    assert r.status_code in (401, 403)


def test_resolve_public_endpoint_needs_no_auth_header():
    """No Authorization header at all — must not 401. A genuinely
    unknown token still 404s (uniform not-found), proving the route
    dispatched (not blocked by auth) rather than merely not-erroring
    for some other reason. get_db/resolve_link mocked so this doesn't
    depend on a real DB connection surviving across TestClient/event
    loop boundaries between test functions (see the two DB-backed
    fixtures in test_presentation_links_service.py for where that's
    actually exercised for real)."""
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_service.resolve_link",
            new=AsyncMock(return_value=SimpleNamespace(status=ResolveResult.NOT_FOUND, data=None)),
        ):
            r = client.get("/api/v1/p/definitely-not-a-real-token")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_unlock_public_endpoint_needs_no_auth_header():
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_token",
            new=AsyncMock(return_value=None),
        ):
            r = client.post("/api/v1/p/definitely-not-a-real-token/unlock", json={"password": "x"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_resolve_maps_not_found_status_to_404():
    client = TestClient(app)
    with patch(
        "app.api.rest.presentation_links.presentation_service.resolve_link",
        new=AsyncMock(return_value=SimpleNamespace(status=ResolveResult.NOT_FOUND, data=None)),
    ):
        r = client.get("/api/v1/p/some-token")
    assert r.status_code == 404


def test_resolve_maps_password_required_to_403_not_404():
    """Distinct from not-found: a password-protected link a viewer
    hasn't unlocked yet must say so, not pretend not to exist."""
    client = TestClient(app)
    with patch(
        "app.api.rest.presentation_links.presentation_service.resolve_link",
        new=AsyncMock(return_value=SimpleNamespace(status=ResolveResult.PASSWORD_REQUIRED, data=None)),
    ):
        r = client.get("/api/v1/p/some-token")
    assert r.status_code == 403
    assert "password" in r.json()["detail"].lower()


def test_resolve_ok_returns_markdown_payload():
    from datetime import datetime
    client = TestClient(app)
    fake_data = {
        "title": "Deck", "markdown": "# Hi", "fileName": "deck.md",
        "revisionId": None, "mode": "live", "publishedAt": datetime.utcnow(),
        "options": {"hideSpeakerNotes": False, "allowDownload": False, "startSlide": 0},
    }
    with patch(
        "app.api.rest.presentation_links.presentation_service.resolve_link",
        new=AsyncMock(return_value=SimpleNamespace(status=ResolveResult.OK, data=fake_data)),
    ):
        r = client.get("/api/v1/p/some-token")
    assert r.status_code == 200
    body = r.json()
    assert body["markdown"] == "# Hi"
    assert body["fileName"] == "deck.md"


def test_unlock_wrong_password_returns_403():
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_link = SimpleNamespace(
        password_hash="$2b$fakehash", revoked_at=None, expires_at=None,
    )
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_token",
            new=AsyncMock(return_value=fake_link),
        ), patch(
            "app.api.rest.presentation_links.verify_password", return_value=False,
        ):
            r = client.post("/api/v1/p/some-token/unlock", json={"password": "wrong"})
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_unlock_correct_password_returns_short_lived_token():
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_link = SimpleNamespace(
        password_hash="$2b$fakehash", revoked_at=None, expires_at=None,
    )
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_token",
            new=AsyncMock(return_value=fake_link),
        ), patch(
            "app.api.rest.presentation_links.verify_password", return_value=True,
        ):
            r = client.post("/api/v1/p/some-token/unlock", json={"password": "right"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert len(body["token"]) > 0
    finally:
        app.dependency_overrides.clear()


def test_resolve_public_endpoint_rate_limited_after_100_per_hour():
    client = TestClient(app)
    with patch(
        "app.api.rest.presentation_links.presentation_service.resolve_link",
        new=AsyncMock(return_value=SimpleNamespace(status=ResolveResult.NOT_FOUND, data=None)),
    ):
        statuses = []
        for _ in range(105):
            r = client.get("/api/v1/p/rate-limit-probe-token")
            statuses.append(r.status_code)
    assert 429 in statuses, f"expected a 429 among {set(statuses)} after exceeding 100/hour"


def test_unlock_rate_limited_after_10_per_hour_per_token():
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_token",
            new=AsyncMock(return_value=None),  # short-circuits to 404 before password check
        ):
            statuses = []
            for _ in range(12):
                r = client.post("/api/v1/p/unlock-rate-probe-token/unlock", json={"password": "x"})
                statuses.append(r.status_code)
        assert 429 in statuses, f"expected a 429 among {set(statuses)} after exceeding 10/hour"
    finally:
        app.dependency_overrides.clear()


def test_revoke_cross_org_admin_cannot_revoke_another_orgs_link():
    """Regression test for a real bug caught via live testing: an org-B
    owner/admin could revoke an org-A link with a 200, because the old
    is_org_admin check used the requester's own roles claim without ever
    confirming the link's file belongs to their org. asset_repo.get_by_id
    is scoped by the REQUESTER's org_id — for a cross-org link it must
    return None, which the endpoint now maps to 404 before any
    creator/admin check runs at all."""
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "22222222-2222-2222-2222-222222222222", "org_id": "org-B", "roles": ["owner"],
    }
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_link = SimpleNamespace(
        id="link-1", file_id="file-in-org-a", created_by="11111111-1111-1111-1111-111111111111",
    )
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_id",
            new=AsyncMock(return_value=fake_link),
        ), patch(
            "app.api.rest.presentation_links.asset_repo.get_by_id",
            new=AsyncMock(return_value=None),  # not found in org-B, the requester's own org
        ) as mock_asset_lookup, patch(
            "app.api.rest.presentation_links.presentation_link_repo.revoke",
            new=AsyncMock(side_effect=AssertionError("must never reach revoke() for a cross-org link")),
        ):
            r = client.delete("/api/v1/presentation-links/link-1")
        assert r.status_code == 404
        mock_asset_lookup.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_revoke_same_org_admin_can_revoke_even_if_not_creator():
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "22222222-2222-2222-2222-222222222222", "org_id": "org-A", "roles": ["admin"],
    }
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_link = SimpleNamespace(
        id="link-1", file_id="file-in-org-a", created_by="11111111-1111-1111-1111-111111111111",
        revoked_at="2026-08-18T00:00:00",
    )
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_id",
            new=AsyncMock(return_value=fake_link),
        ), patch(
            "app.api.rest.presentation_links.asset_repo.get_by_id",
            new=AsyncMock(return_value=SimpleNamespace(id="file-in-org-a")),  # found: same org
        ), patch(
            "app.api.rest.presentation_links.presentation_link_repo.revoke",
            new=AsyncMock(return_value=fake_link),
        ):
            r = client.delete("/api/v1/presentation-links/link-1")
        assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_revoke_same_org_non_creator_non_admin_gets_403_not_200():
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "33333333-3333-3333-3333-333333333333", "org_id": "org-A", "roles": ["editor"],
    }
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    fake_link = SimpleNamespace(
        id="link-1", file_id="file-in-org-a", created_by="11111111-1111-1111-1111-111111111111",
    )
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_id",
            new=AsyncMock(return_value=fake_link),
        ), patch(
            "app.api.rest.presentation_links.asset_repo.get_by_id",
            new=AsyncMock(return_value=SimpleNamespace(id="file-in-org-a")),  # same org, but not creator/admin
        ), patch(
            "app.api.rest.presentation_links.presentation_link_repo.revoke",
            new=AsyncMock(side_effect=AssertionError("must never reach revoke() without permission")),
        ):
            r = client.delete("/api/v1/presentation-links/link-1")
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_unlock_rate_limit_is_per_token_not_shared_globally():
    """Two different link tokens each get their own 10/hour bucket
    (key_func combines IP+token) — probing token A up to its limit must
    not affect token B's own budget."""
    client = TestClient(app)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch(
            "app.api.rest.presentation_links.presentation_link_repo.get_by_token",
            new=AsyncMock(return_value=None),
        ):
            for _ in range(10):
                client.post("/api/v1/p/token-a-probe/unlock", json={"password": "x"})
            r = client.post("/api/v1/p/token-b-probe/unlock", json={"password": "x"})
        assert r.status_code != 429
    finally:
        app.dependency_overrides.clear()
