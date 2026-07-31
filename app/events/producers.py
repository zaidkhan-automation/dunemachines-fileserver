"""
Event producers — publish events to Redis pub/sub.
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict
from app.core.redis import get_redis
from app.events.event_types import EventType

logger = logging.getLogger(__name__)


async def publish_event(event_type: EventType, payload: Dict[str, Any], channel: str = "fileserver:events"):
    """Publish event to Redis pub/sub channel."""
    try:
        redis = await get_redis()
        event = {
            "type": event_type.value,
            "payload": payload,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await redis.publish(channel, json.dumps(event))
        logger.debug(f"Event published: {event_type.value}")
    except Exception as e:
        logger.error(f"Failed to publish event {event_type.value}: {e}")


async def publish_asset_created(asset_id: str, org_id: str, asset_type: str):
    await publish_event(EventType.ASSET_CREATED, {
        "asset_id": asset_id,
        "org_id": org_id,
        "asset_type": asset_type,
    })


async def publish_upload_complete(asset_id: str, org_id: str, object_key: str):
    await publish_event(EventType.ASSET_UPLOAD_COMPLETE, {
        "asset_id": asset_id,
        "org_id": org_id,
        "object_key": object_key,
    })
