"""F-10 remediation — app/api/graphql/schema.py's get_context() used to
swallow both the native-token and Duniverse-token auth failures with a
bare `except Exception: pass`, indistinguishable in the logs from a
resolve_identity() outage (DB/Redis down). Client-facing behavior was
already fail-closed (every resolver does `if not user: return None`,
confirmed by reading Query.asset) — this only adds logging, never changes
what the client sees.
"""
import logging

import pytest

from app.api.graphql.schema import get_context


class _FakeState:
    pass


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers
        self.state = _FakeState()


@pytest.mark.asyncio
async def test_no_auth_header_returns_none_user_no_log(caplog):
    request = _FakeRequest(headers={})
    with caplog.at_level(logging.DEBUG):
        ctx = await get_context(request)
    assert request.state.user is None
    assert ctx == {"request": request}
    assert caplog.records == []  # nothing to log — no bearer token was even presented


@pytest.mark.asyncio
async def test_malformed_token_fails_closed_and_logs_both_attempts(caplog):
    request = _FakeRequest(headers={"authorization": "Bearer not-a-real-jwt"})
    with caplog.at_level(logging.DEBUG):
        ctx = await get_context(request)
    assert request.state.user is None
    assert ctx == {"request": request}
    # Both the native and Duniverse decode attempts must have logged something.
    messages = [r.getMessage() for r in caplog.records]
    assert any("native token auth failed" in m for m in messages)
    assert any("both token types" in m for m in messages)


@pytest.mark.asyncio
async def test_malformed_token_never_logs_the_token_itself(caplog):
    secret_looking_token = "Bearer eyThisShouldNeverAppearInLogsXYZ123"
    request = _FakeRequest(headers={"authorization": secret_looking_token})
    with caplog.at_level(logging.DEBUG):
        await get_context(request)
    full_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ThisShouldNeverAppearInLogsXYZ123" not in full_log_text


@pytest.mark.asyncio
async def test_resolve_identity_outage_is_logged_not_silently_swallowed(caplog, monkeypatch):
    """Simulates the exact scenario F-10 was about: resolve_identity()
    raising for an infrastructure reason (not a bad token) must be
    visible in the logs, not indistinguishable from a routine decode
    failure."""
    import app.core.security as security_module

    async def _broken_resolve_identity(payload, trust_claims=True):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(security_module, "resolve_identity", _broken_resolve_identity)

    # A token that decodes successfully (native, self-issued) so we reach
    # resolve_identity() rather than failing at decode.
    token = security_module.create_access_token({"sub": "test-user", "issuer": "fileserver"})
    request = _FakeRequest(headers={"authorization": f"Bearer {token}"})

    with caplog.at_level(logging.DEBUG):
        ctx = await get_context(request)

    assert request.state.user is None  # still fails closed
    assert ctx == {"request": request}
    messages = [r.getMessage() for r in caplog.records]
    assert any("simulated DB outage" in m or "RuntimeError" in m for m in messages), messages
