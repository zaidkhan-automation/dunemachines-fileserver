"""WS auth negative cases for app/api/ws/collaboration.py's
websocket_endpoint — missing/malformed/forged tokens must all be cleanly
rejected (accept() + {"type":"error"} + close(code=4001)), never silently
falling through to an anonymous/default identity. authenticate_ws()
returns None for all three without ever reaching resolve_identity's DB
lookup (confirmed by reading it: `if not token: return None` short-
circuits immediately; a bad-signature token raises inside decode_token/
decode_duniverse_token before resolve_identity is called) — so, same as
tests/test_websocket_signals.py's own TestClient(app)-without-lifespan
convention, no real DB/Redis dependency is needed for these three cases.
"""
import jwt as pyjwt
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from main import app
from app.core.config import settings
from app.core.security import create_access_token

client = TestClient(app)


def _expect_rejected(token_query: str):
    with client.websocket_connect(f"/ws{token_query}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "Unauthorized"
        try:
            ws.receive_text()
            assert False, "connection should be closed after the Unauthorized error"
        except WebSocketDisconnect as exc:
            assert exc.code == 4001


def test_missing_token_rejected():
    _expect_rejected("")


def test_empty_token_rejected():
    _expect_rejected("?token=")


def test_malformed_token_rejected():
    _expect_rejected("?token=not.a.real.jwt")


def test_forged_signature_token_rejected():
    """Signed with a secret the server doesn't recognize at all — must
    fail both decode_token (JWT_SECRET) and decode_duniverse_token
    (DUNIVERSE_JWT_SECRET), regardless of the current X-03 finding that
    those two happen to be equal in this deployment; a THIRD, unrelated
    secret proves the rejection isn't accidentally permissive."""
    forged = pyjwt.encode(
        {"sub": "attacker", "org_id": "00000000-0000-0000-0000-00000000dead", "roles": ["owner"]},
        "definitely-not-the-real-secret-xyz", algorithm=settings.JWT_ALGORITHM,
    )
    _expect_rejected(f"?token={forged}")


def test_expired_token_rejected():
    import time
    expired = pyjwt.encode(
        {"sub": "some-user", "org_id": "00000000-0000-0000-0000-000000000001",
         "issuer": "fileserver", "iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600},
        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
    )
    _expect_rejected(f"?token={expired}")


def test_valid_native_token_is_accepted_not_rejected():
    """Control case — proves _expect_rejected's own machinery is
    actually discriminating pass vs. fail, not just always matching a
    close event regardless of token validity."""
    import uuid
    token = create_access_token({"sub": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "roles": ["editor"]})
    with client.websocket_connect(f"/ws?token={token}") as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"
