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

# event_type.value -> list of handler coroutine functions. Must be a list,
# not a single slot — app.workers.ai.embeddings and app.workers.files.thumbnails
# both subscribe to EventType.ASSET_UPLOAD_COMPLETE (one does text/embedding
# extraction and is the only code that ever advances status to READY; the
# other does thumbnail generation). A single-slot dict silently let whichever
# module was imported last (thumbnails, per _load_workers()'s import order)
# overwrite the other's registration, so the embeddings handler — and the
# READY status transition — never ran for a single asset since this repo's
# initial commit (2026-08-19 production investigation: 378 assets stuck in
# 'processing' going back to 2026-05-19, 100% reproducible, zero errors
# logged since nothing was actually failing/throwing).
HANDLERS: dict[str, list] = {}


def on_event(event_type: EventType):
    """Decorator to register an event handler. Multiple handlers may
    subscribe to the same event type — all of them run."""
    def decorator(func):
        HANDLERS.setdefault(event_type.value, []).append(func)
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
                    for handler in HANDLERS.get(event_type, []):
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
