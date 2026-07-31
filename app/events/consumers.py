"""
Event consumers — subscribe to Redis pub/sub and dispatch to workers.
Auto-reconnects on connection drop.
"""
import json
import asyncio
import logging
from app.core.redis import get_redis
from app.events.event_types import EventType

logger = logging.getLogger(__name__)

HANDLERS = {}


def on_event(event_type: EventType):
    """Decorator to register event handler."""
    def decorator(func):
        HANDLERS[event_type.value] = func
        return func
    return decorator


async def start_consumer(channel: str = "fileserver:events"):
    """Start event consumer loop with auto-reconnect."""
    logger.info(f"Event consumer started on channel: {channel}")

    while True:
        try:
            redis = await get_redis()
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            logger.info(f"Redis pubsub subscribed to: {channel}")

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                    event_type = event.get("type")
                    handler = HANDLERS.get(event_type)
                    if handler:
                        asyncio.create_task(handler(event["payload"]))
                except Exception as e:
                    logger.error(f"Event consumer dispatch error: {e}")

        except Exception as e:
            logger.warning(f"Redis pubsub connection lost: {e} — reconnecting in 5s...")
            await asyncio.sleep(5)
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:
                pass
