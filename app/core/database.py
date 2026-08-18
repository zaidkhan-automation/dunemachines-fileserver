import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=40,
    # Hygiene additions (2026-08 pool audit — live-measured connection count
    # was 15-17 total across all DBs sharing this Postgres instance, nowhere
    # near the 100 max_connections ceiling or even pool_size, so this is
    # NOT a response to any measured exhaustion). pool_pre_ping issues a
    # cheap SELECT 1 before handing out a pooled connection, so a
    # connection killed server-side (idle timeout, restart) is detected
    # and replaced instead of surfacing as an OperationalError on the
    # caller's real query. pool_recycle forces connections older than an
    # hour to be replaced, ahead of any server-side max-lifetime setting.
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    pass

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


_POOL_LOG_INTERVAL_SECONDS = 300  # 5 min — cheap, in-process pool.status(), no DB round trip


async def log_pool_status_periodically():
    """Background task (started from app/core/events.py's on_startup) —
    logs this engine's own connection-pool usage so a real exhaustion
    trend (checked-out count approaching pool_size+max_overflow) shows
    up in logs before it causes timeouts, instead of only being
    discoverable by someone manually querying pg_stat_activity."""
    pool = engine.pool
    while True:
        try:
            logger.info(
                "[db_pool] size=%d checked_out=%d overflow=%d checked_in=%d",
                pool.size(), pool.checkedout(), pool.overflow(), pool.checkedin(),
            )
        except Exception as e:
            logger.warning(f"[db_pool] status check failed: {e}")
        await asyncio.sleep(_POOL_LOG_INTERVAL_SECONDS)
