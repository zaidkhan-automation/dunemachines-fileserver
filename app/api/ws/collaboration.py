"""
WebSocket — Realtime collaboration, presence, notifications.
Handles:
- Asset updates (status changes, new uploads)
- Presence (who is online, who is viewing what)
- Realtime notifications
- File processing status updates
"""
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, Set, Optional
from fastapi import WebSocket, WebSocketDisconnect, APIRouter, Query
from app.core.security import decode_token, decode_duniverse_token, resolve_identity

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Connection Manager ────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        # org_id -> set of websockets
        self.org_connections: Dict[str, Set[WebSocket]] = {}
        # ws -> user info
        self.ws_users: Dict[WebSocket, dict] = {}
        # asset_id -> set of websockets (who is viewing this asset)
        self.asset_viewers: Dict[str, Set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user: dict):
        await ws.accept()
        org_id = user["org_id"]

        if org_id not in self.org_connections:
            self.org_connections[org_id] = set()
        self.org_connections[org_id].add(ws)
        self.ws_users[ws] = user

        logger.info(f"WS connected: {user.get('email')} org={org_id} total={len(self.org_connections[org_id])}")

        # Send welcome
        await ws.send_json({
            "type": "connected",
            "user_id": user["user_id"],
            "org_id": org_id,
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Broadcast presence to org
        await self.broadcast_to_org(org_id, {
            "type": "presence.joined",
            "user_id": user["user_id"],
            "email": user.get("email", ""),
            "timestamp": datetime.utcnow().isoformat(),
        }, exclude=ws)

    async def disconnect(self, ws: WebSocket):
        user = self.ws_users.pop(ws, {})
        org_id = user.get("org_id", "")

        if org_id in self.org_connections:
            self.org_connections[org_id].discard(ws)
            if not self.org_connections[org_id]:
                del self.org_connections[org_id]

        # Remove from asset viewers
        for viewers in self.asset_viewers.values():
            viewers.discard(ws)

        if user:
            logger.info(f"WS disconnected: {user.get('email')}")
            await self.broadcast_to_org(org_id, {
                "type": "presence.left",
                "user_id": user.get("user_id"),
                "email": user.get("email", ""),
                "timestamp": datetime.utcnow().isoformat(),
            })

    async def broadcast_to_org(self, org_id: str, message: dict, exclude: Optional[WebSocket] = None):
        """Send message to all connections in an org."""
        connections = self.org_connections.get(org_id, set()).copy()
        dead = set()
        for ws in connections:
            if ws == exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def send_to_user(self, org_id: str, user_id: str, message: dict):
        """Send message to specific user."""
        connections = self.org_connections.get(org_id, set()).copy()
        for ws in connections:
            user = self.ws_users.get(ws, {})
            if user.get("user_id") == user_id:
                try:
                    await ws.send_json(message)
                except Exception:
                    await self.disconnect(ws)

    async def notify_asset_update(self, org_id: str, asset_id: str, status: str, extra: dict = None):
        """Notify org about asset status change."""
        await self.broadcast_to_org(org_id, {
            "type": "asset.updated",
            "asset_id": asset_id,
            "status": status,
            "extra": extra or {},
            "timestamp": datetime.utcnow().isoformat(),
        })

    def get_online_users(self, org_id: str) -> list:
        """Get list of online users in org."""
        connections = self.org_connections.get(org_id, set())
        users = []
        for ws in connections:
            user = self.ws_users.get(ws, {})
            if user:
                users.append({
                    "user_id": user.get("user_id"),
                    "email": user.get("email", ""),
                })
        return users

    def total_connections(self) -> int:
        return sum(len(v) for v in self.org_connections.values())


manager = ConnectionManager()


# ── Auth helper ───────────────────────────────────────────────────

async def authenticate_ws(token: str) -> Optional[dict]:
    """Authenticate WebSocket connection via JWT."""
    if not token:
        return None
    try:
        payload = decode_token(token)
        identity = resolve_identity(payload)
        return {
            "user_id": identity["user_id"],
            "org_id": identity["org_id"],
            "email": payload.get("email", ""),
            "roles": payload.get("roles", []),
        }
    except Exception:
        pass
    try:
        payload = decode_duniverse_token(token)
        identity = resolve_identity(payload)
        return {
            "user_id": identity["user_id"],
            "org_id": identity["org_id"],
            "email": payload.get("email", ""),
            "roles": payload.get("roles", []),
        }
    except Exception:
        return None


# ── Message Handlers ──────────────────────────────────────────────

async def handle_message(ws: WebSocket, user: dict, data: dict):
    """Handle incoming WebSocket messages."""
    msg_type = data.get("type", "")

    if msg_type == "ping":
        await ws.send_json({"type": "pong", "timestamp": datetime.utcnow().isoformat()})

    elif msg_type == "presence.get":
        users = manager.get_online_users(user["org_id"])
        await ws.send_json({"type": "presence.list", "users": users, "count": len(users)})

    elif msg_type == "asset.watch":
        asset_id = data.get("asset_id")
        if asset_id:
            if asset_id not in manager.asset_viewers:
                manager.asset_viewers[asset_id] = set()
            manager.asset_viewers[asset_id].add(ws)
            await ws.send_json({"type": "asset.watching", "asset_id": asset_id})

    elif msg_type == "asset.unwatch":
        asset_id = data.get("asset_id")
        if asset_id and asset_id in manager.asset_viewers:
            manager.asset_viewers[asset_id].discard(ws)
            await ws.send_json({"type": "asset.unwatched", "asset_id": asset_id})

    elif msg_type == "message.broadcast":
        # Broadcast message to org
        text = data.get("text", "")
        if text:
            await manager.broadcast_to_org(user["org_id"], {
                "type": "message.received",
                "from_user": user["user_id"],
                "from_email": user.get("email", ""),
                "text": text[:500],
                "timestamp": datetime.utcnow().isoformat(),
            })

    else:
        await ws.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})


# ── WebSocket Endpoint ────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    token: str = Query(default=""),
):
    """
    Main WebSocket endpoint.
    Connect: ws://host:8007/ws?token=<jwt>

    Message types (client → server):
    - ping → pong
    - presence.get → presence.list
    - asset.watch {asset_id} → asset.watching
    - asset.unwatch {asset_id} → asset.unwatched
    - message.broadcast {text} → message.received (to all in org)

    Events (server → client):
    - connected
    - presence.joined / presence.left
    - asset.updated (status change, processing complete)
    - message.received
    - notification
    """
    user = await authenticate_ws(token)
    if not user:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "Unauthorized"})
        await ws.close(code=4001)
        return

    await manager.connect(ws, user)

    # Subscribe to Redis events for this org
    redis_task = asyncio.create_task(_redis_listener(ws, user))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
                await handle_message(ws, user, data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WS error: {e}")
    finally:
        redis_task.cancel()
        await manager.disconnect(ws)


async def _redis_listener(ws: WebSocket, user: dict):
    """Listen to Redis events and forward relevant ones to this WS client."""
    try:
        from app.core.redis import get_redis
        redis = await get_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe("fileserver:events")

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                event = json.loads(message["data"])
                event_type = event.get("type", "")
                payload = event.get("payload", {})

                # Forward asset events to the org
                if event_type in [
                    "asset.upload_complete",
                    "asset.processing_complete",
                    "asset.processing_failed",
                    "asset.created",
                    "asset.updated",
                    "asset.deleted",
                    "file.write",
                    "file.edit",
                ] and payload.get("org_id") == user["org_id"]:
                    await ws.send_json({
                        "type": event_type,
                        "payload": payload,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Redis listener error: {e}")
