"""
Integration tests for the real-time WebSocket signal system
(app/api/ws/collaboration.py) — connection auth, watch/unwatch, and
Redis-pub/sub-backed event delivery (asset.upload_complete,
asset.updated) end to end, against a REAL local Redis instance
(REDIS_URL, fileserver:events channel) — no mocking of the pub/sub
layer, since that's the exact mechanism under test.

Same TestClient(app)-without-lifespan convention as
tests/test_presentation_links_router.py: no DB/S3/Redis startup hook
runs, so tokens are minted via create_access_token with a UUID `sub`
and no numeric `uid` claim — resolve_identity() then has nothing to
look up (no omnius_db call) and trusts the token's own org_id claim
directly, same as any issuer="fileserver" native token.

Redis-client-per-loop caveat: with no `with TestClient(app) as client:`
lifespan, starlette gives EVERY websocket_connect() call its own fresh
anyio portal thread + event loop (TestClient._portal_factory only
reuses one shared portal when the lifespan context is active). Since
app.core.redis.get_redis() caches a single global client, reusing it
across two different loops raises "attached to a different loop" —
so _reset_redis_client() is called immediately before every single
websocket_connect() and before every publish, forcing a fresh
connection bound to whichever loop is about to use it. This is a test
harness constraint, not a production behavior — real deployments run
one process/one loop, so get_redis()'s caching is correct there.
"""
import asyncio
import time

from fastapi.testclient import TestClient

import app.core.redis as redis_module
from main import app
from app.core.security import create_access_token
from app.events.event_types import EventType
from app.events.producers import publish_event
from app.api.ws.collaboration import manager


def _token(org_id: str, user_id: str = None, roles=("editor",)) -> str:
    import uuid
    return create_access_token({
        "sub": user_id or str(uuid.uuid4()),
        "org_id": org_id,
        "roles": list(roles),
    })


def _reset_redis_client():
    redis_module._redis_client = None


async def _publish(event_type: EventType, payload: dict):
    _reset_redis_client()
    await publish_event(event_type, payload)
    if redis_module._redis_client is not None:
        try:
            await redis_module._redis_client.close()
        except Exception:
            pass
    _reset_redis_client()


def _publish_sync(event_type: EventType, payload: dict):
    asyncio.run(_publish(event_type, payload))


def _drain_until(ws, msg_type: str, tries: int = 8) -> dict:
    """Read messages off the socket, skipping incidental ones (presence
    broadcasts, pongs), until msg_type is found or `tries` is exhausted."""
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("type") == msg_type:
            return msg
    raise AssertionError(f"did not receive a {msg_type!r} message within {tries} tries")


def _connect(client: TestClient, org_id: str, user_id: str = None):
    _reset_redis_client()
    token = _token(org_id, user_id=user_id)
    return client.websocket_connect(f"/ws?token={token}")


def test_websocket_connects_with_valid_token():
    client = TestClient(app)
    import uuid
    org_id = str(uuid.uuid4())
    with _connect(client, org_id) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "connected"
        assert msg["org_id"] == org_id


def test_websocket_rejects_invalid_token():
    client = TestClient(app)
    with client.websocket_connect("/ws?token=not-a-real-jwt") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "Unauthorized"


def test_watch_and_unwatch_asset():
    import uuid
    client = TestClient(app)
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    with _connect(client, org_id) as ws:
        ws.receive_json()  # connected

        ws.send_json({"type": "asset.watch", "asset_id": asset_id})
        msg = ws.receive_json()
        assert msg == {"type": "asset.watching", "asset_id": asset_id}
        # ws here is the client-side WebSocketTestSession handle, not the
        # server-side starlette.websockets.WebSocket instance the manager
        # actually stores — asset_id is unique per test, so a count check
        # is an equally strong assertion without needing object identity.
        assert len(manager.asset_viewers.get(asset_id, set())) == 1

        ws.send_json({"type": "asset.unwatch", "asset_id": asset_id})
        msg = ws.receive_json()
        assert msg == {"type": "asset.unwatched", "asset_id": asset_id}
        assert len(manager.asset_viewers.get(asset_id, set())) == 0


def test_upload_complete_signal_delivered():
    import uuid
    client = TestClient(app)
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    with _connect(client, org_id) as ws:
        ws.receive_json()  # connected
        time.sleep(0.3)  # let the Redis SUBSCRIBE land before we publish

        _publish_sync(EventType.ASSET_UPLOAD_COMPLETE, {
            "asset_id": asset_id,
            "org_id": org_id,
            "object_key": "orgs/x/assets/x/file.dm-doc",
        })

        msg = _drain_until(ws, "asset.upload_complete")
        assert msg["payload"]["asset_id"] == asset_id
        assert msg["payload"]["org_id"] == org_id


def test_status_ready_signal_delivered():
    import uuid
    client = TestClient(app)
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    with _connect(client, org_id) as ws:
        ws.receive_json()  # connected
        time.sleep(0.3)

        _publish_sync(EventType.ASSET_UPDATED, {
            "asset_id": asset_id,
            "org_id": org_id,
            "status": "ready",
        })

        msg = _drain_until(ws, "asset.updated")
        assert msg["payload"]["asset_id"] == asset_id
        assert msg["payload"]["status"] == "ready"


def test_multiple_subscribers_receive_same_signal():
    import uuid
    client = TestClient(app)
    org_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    with _connect(client, org_id) as ws1:
        ws1.receive_json()  # connected
        with _connect(client, org_id) as ws2:
            ws2.receive_json()  # connected
            time.sleep(0.3)

            _publish_sync(EventType.ASSET_UPLOAD_COMPLETE, {
                "asset_id": asset_id,
                "org_id": org_id,
                "object_key": "orgs/x/assets/x/file.dm-doc",
            })

            msg1 = _drain_until(ws1, "asset.upload_complete")
            msg2 = _drain_until(ws2, "asset.upload_complete")
            assert msg1["payload"]["asset_id"] == asset_id
            assert msg2["payload"]["asset_id"] == asset_id


def test_org_isolation_cross_org_signal_not_delivered():
    import uuid
    client = TestClient(app)
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())
    with _connect(client, org_a) as ws_a:
        ws_a.receive_json()  # connected
        with _connect(client, org_b) as ws_b:
            ws_b.receive_json()  # connected
            time.sleep(0.3)

            _publish_sync(EventType.ASSET_UPLOAD_COMPLETE, {
                "asset_id": asset_id,
                "org_id": org_a,
                "object_key": "orgs/x/assets/x/file.dm-doc",
            })

            # org_a's connection gets it...
            msg_a = _drain_until(ws_a, "asset.upload_complete")
            assert msg_a["payload"]["org_id"] == org_a

            # ...org_b's must not. Ping/pong as an ordering barrier: if the
            # event had leaked to org_b, it would be queued ahead of the
            # pong (message order is preserved per-connection), so the
            # very next message being "pong" proves nothing leaked —
            # without needing a hard receive timeout.
            ws_b.send_json({"type": "ping"})
            msg_b = ws_b.receive_json()
            assert msg_b["type"] == "pong"


def test_disconnection_cleanup():
    import uuid
    client = TestClient(app)
    org_id = str(uuid.uuid4())
    with _connect(client, org_id) as ws:
        ws.receive_json()  # connected
        # ws is the client-side handle; the manager keys its dicts by the
        # server-side WebSocket instance, so assert via the unique org_id
        # instead of object identity.
        assert len(manager.org_connections.get(org_id, set())) == 1

    # WebSocketTestSession.__exit__ closes the socket and tears down its
    # portal, which blocks until the server-side task (including the
    # endpoint's `finally: await manager.disconnect(ws)`) has finished —
    # but poll briefly anyway rather than assuming exact timing.
    deadline = time.time() + 2
    while time.time() < deadline and org_id in manager.org_connections:
        time.sleep(0.05)

    assert org_id not in manager.org_connections
