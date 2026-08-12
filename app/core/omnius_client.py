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

Short TTL cache bounds both the staleness window and the query volume
— every authenticated request would otherwise hit omnius_db, and a
switch is still effectively "immediate" from a user's perspective at a
few seconds of cache lag, versus the old 15-minute token-refresh-bound
propagation.

This process runs with uvicorn workers=2 in production (main.py), so
the cache lives in Redis rather than a local dict — a local dict would
give each worker its own independently-expiring snapshot of a user's
active org, and since requests land on either worker essentially at
random, a user switching orgs would see results flap between the old
and new org (whichever worker's cache hadn't expired yet) instead of
flipping once, consistently, for both.
"""

import asyncpg
import logging
import time
from typing import Optional

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_last_fail_ts: float = 0.0
_FAIL_COOLDOWN_SECONDS = 60  # avoid hammering a down/misconfigured omnius_db every request

_CACHE_KEY_PREFIX = "fileserver:active_org:"
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


# Sentinel stored in Redis to distinguish "looked up, user has no active
# org" (cache this — don't re-query every request) from "not cached yet"
# (Redis GET returning None for a missing key).
_NO_ACTIVE_ORG_SENTINEL = "__none__"


async def get_active_org_id(user_id: int) -> Optional[str]:
    """user_id is app_users.id (duniverse_db) / omnius_db's own bigint ids
    for agent tokens — same key user_active_org keys on. Returns None on
    any failure, cache miss beyond TTL aside, or if the user has no
    active org set.

    Cached in Redis (not a local dict) because this process runs with
    multiple uvicorn workers — see module docstring."""
    cache_key = f"{_CACHE_KEY_PREFIX}{user_id}"

    try:
        redis_client = await get_redis()
        cached = await redis_client.get(cache_key)
        if cached is not None:
            return None if cached == _NO_ACTIVE_ORG_SENTINEL else cached
    except Exception as e:
        logger.warning(f"active-org cache read failed for user_id={user_id}: {e}")

    try:
        pool = await _get_pool()
        if pool is None:
            return None
        async with pool.acquire() as conn:
            org_id = await conn.fetchval(
                "SELECT org_id FROM user_active_org WHERE user_id = $1", user_id
            )
            result = str(org_id) if org_id else None
    except Exception as e:
        logger.warning(f"active-org lookup failed for user_id={user_id}: {e}")
        return None

    try:
        redis_client = await get_redis()
        await redis_client.set(
            cache_key, result if result is not None else _NO_ACTIVE_ORG_SENTINEL,
            ex=_CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning(f"active-org cache write failed for user_id={user_id}: {e}")

    return result
