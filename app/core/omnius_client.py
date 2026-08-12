"""
Live read into omnius_db's user_active_org table, so resolve_identity()
can resolve a bridged user's org_id fresh on every request instead of
trusting a snapshot baked into the JWT at issue time. This is the
primary org_id source now (see resolve_identity in security.py) — the
org_id claim on the token, if any, is only a fail-open fallback for
when this lookup can't reach omnius_db.

Fails open: any problem reaching omnius_db must never block auth, it
just means resolve_identity falls back to the token's own org_id claim
(if present) or its uuid5 derivation, exactly as before this existed.

Short in-process TTL cache bounds both the staleness window and the
query volume — every authenticated request would otherwise hit
omnius_db, and a switch is still effectively "immediate" from a user's
perspective at a few seconds of cache lag, versus the old 15-minute
token-refresh-bound propagation.
"""

import asyncpg
import logging
import time
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_last_fail_ts: float = 0.0
_FAIL_COOLDOWN_SECONDS = 60  # avoid hammering a down/misconfigured omnius_db every request

_cache: dict[int, tuple[Optional[str], float]] = {}
_CACHE_TTL_SECONDS = 5


async def _get_pool() -> Optional[asyncpg.Pool]:
    global _pool, _last_fail_ts

    if _pool is not None:
        return _pool

    if time.monotonic() - _last_fail_ts < _FAIL_COOLDOWN_SECONDS:
        return None

    db_url = settings.OMNIUS_DB_URL
    if not db_url:
        _last_fail_ts = time.monotonic()
        return None

    try:
        _pool = await asyncpg.create_pool(
            dsn=db_url, min_size=1, max_size=5, command_timeout=3, timeout=2
        )
        return _pool
    except Exception as e:
        logger.warning(f"omnius_db pool init failed, falling back to token org_id claim: {e}")
        _last_fail_ts = time.monotonic()
        return None


async def get_active_org_id(user_id: int) -> Optional[str]:
    """user_id is app_users.id (duniverse_db) / omnius_db's own bigint ids
    for agent tokens — same key user_active_org keys on. Returns None on
    any failure, cache miss beyond TTL aside, or if the user has no
    active org set."""
    now = time.monotonic()
    cached = _cache.get(user_id)
    if cached is not None and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    try:
        pool = await _get_pool()
        if pool is None:
            return cached[0] if cached is not None else None
        async with pool.acquire() as conn:
            org_id = await conn.fetchval(
                "SELECT org_id FROM user_active_org WHERE user_id = $1", user_id
            )
            result = str(org_id) if org_id else None
    except Exception as e:
        logger.warning(f"active-org lookup failed for user_id={user_id}: {e}")
        return cached[0] if cached is not None else None

    _cache[user_id] = (result, now)
    return result
