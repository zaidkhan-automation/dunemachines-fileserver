"""
Application lifecycle events — startup/shutdown hooks.
"""
import asyncio
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

_pool_monitor_task: asyncio.Task | None = None


async def on_startup():
    logger.info(f"Starting {settings.APP_NAME} v{settings.VERSION}")
    await _init_database()
    await _init_redis()
    await _init_storage()
    await _load_workers()
    await _start_event_consumer()
    # Warm up embedding model in background — avoids first-request timeout
    asyncio.create_task(_warmup_models())
    global _pool_monitor_task
    from app.core.database import log_pool_status_periodically
    _pool_monitor_task = asyncio.create_task(log_pool_status_periodically())
    logger.info("All services initialized ✅")


async def on_shutdown():
    logger.info("Shutting down services...")
    if _pool_monitor_task:
        _pool_monitor_task.cancel()
    from app.core.redis import close_redis
    await close_redis()
    logger.info("Shutdown complete")


async def _init_database():
    try:
        from app.core.database import init_db
        await init_db()
        logger.info("Database initialized ✅")
    except Exception as e:
        logger.error(f"Database init failed: {e}")


async def _init_redis():
    try:
        from app.core.redis import get_redis
        r = await get_redis()
        await r.ping()
        logger.info("Redis connected ✅")
    except Exception as e:
        logger.warning(f"Redis not available: {e}")


async def _init_storage():
    try:
        import aioboto3
        from app.core.config import settings
        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=settings.STORAGE_ENDPOINT,
            aws_access_key_id=settings.STORAGE_ACCESS_KEY,
            aws_secret_access_key=settings.STORAGE_SECRET_KEY,
        ) as s3:
            buckets = await s3.list_buckets()
            bucket_names = [b["Name"] for b in buckets.get("Buckets", [])]
            if settings.STORAGE_BUCKET not in bucket_names:
                await s3.create_bucket(Bucket=settings.STORAGE_BUCKET)
                logger.info(f"Created bucket: {settings.STORAGE_BUCKET}")
        logger.info("MinIO storage initialized ✅")
    except Exception as e:
        logger.warning(f"Storage not available: {e}")


async def _load_workers():
    """Import workers to register event handlers."""
    try:
        import app.workers.ai.embeddings
        import app.workers.ai.summaries
        import app.workers.files.thumbnails
        logger.info("Workers loaded ✅")
    except Exception as e:
        logger.warning(f"Worker loading failed: {e}")


async def _warmup_models():
    """Pre-load embedding model so first request is fast."""
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        from app.services.ai.embeddings import get_model
        await loop.run_in_executor(None, get_model)
        logger.info("Embedding model warmed up ✅")
    except Exception as e:
        logger.warning(f"Model warmup failed: {e}")


async def _start_event_consumer():
    """Start Redis event consumer in background."""
    try:
        from app.events.consumers import start_consumer
        asyncio.create_task(start_consumer())
        logger.info("Event consumer started ✅")
    except Exception as e:
        logger.warning(f"Event consumer failed: {e}")
