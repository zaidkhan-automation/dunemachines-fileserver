"""
Global request-rate limiter (slowapi). RATE_LIMIT_SEARCH/RATE_LIMIT_UPLOAD
already existed in Settings but were never wired into any actual
enforcement anywhere in the service (security audit finding, 2026-08) —
this module is the enforcement side.

storage_uri points at the same Redis instance the rest of the app already
uses (app.core.redis, REDIS_URL) — with no storage_uri, slowapi/limits
defaults to an in-process dict, and this app runs uvicorn with workers=2
(see main.py), so each worker kept its own independent counter: a
"10/hour" limit was really ~10/hour PER WORKER, and which worker served a
given request (so which counter it hit) is non-deterministic, making the
effective ceiling both higher than configured and inconsistent (QA
finding, 2026-08-24: 13 requests against a 10/hour limit got zero 429s in
one run). Redis storage makes the counters actually global across
workers/processes, which is what the configured limits were meant to
mean. `limits`' RedisStorage uses the plain sync `redis` client already
in requirements.txt (redis[asyncio]==5.1.0 — the [asyncio] extra only
adds the async client on top; the base sync client ships in the same
package) and needs no additional dependency.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.config import settings

limiter = Limiter(key_func=get_remote_address, storage_uri=settings.REDIS_URL)
