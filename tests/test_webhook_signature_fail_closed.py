"""
Finding #13 (corrected): GitHub webhook signature verification is
already constant-time (hmac.compare_digest) — the real gap was that
verification was skipped entirely when GITHUB_WEBHOOK_SECRET is an
empty string, instead of rejecting every request.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.connectors.github.webhook_handler import handle_github_webhook, verify_signature


def _fake_request(body: bytes) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "headers": []}, receive)


def test_verify_signature_uses_constant_time_comparison():
    # Already correct — confirms hmac.compare_digest is what's used, not a
    # plain == string comparison.
    import inspect

    src = inspect.getsource(verify_signature)
    assert "hmac.compare_digest" in src


@pytest.mark.asyncio
async def test_empty_secret_rejects_instead_of_skipping_verification():
    with patch("app.connectors.github.webhook_handler.settings") as mock_settings:
        mock_settings.GITHUB_WEBHOOK_SECRET = ""
        with pytest.raises(HTTPException) as exc_info:
            await handle_github_webhook(
                _fake_request(b'{"zen": "test"}'),
                x_github_event="ping",
                x_hub_signature_256="sha256=whatever",
                x_github_delivery="test-delivery-1",
            )
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_signature_with_real_secret_still_rejected():
    with patch("app.connectors.github.webhook_handler.settings") as mock_settings:
        mock_settings.GITHUB_WEBHOOK_SECRET = "real-secret"
        with pytest.raises(HTTPException) as exc_info:
            await handle_github_webhook(
                _fake_request(b'{"zen": "test"}'),
                x_github_event="ping",
                x_hub_signature_256="sha256=deadbeef",
                x_github_delivery="test-delivery-2",
            )
        assert exc_info.value.status_code == 401
