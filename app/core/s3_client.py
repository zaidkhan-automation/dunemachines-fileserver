"""
Singleton MinIO/S3 client — held open for the process lifetime instead of
built fresh per call.

Every prior call site did `aioboto3.Session()` + `async with
session.client(...)` on every single invocation. aioboto3's `.client()` is
not a cheap object — entering it sets up a real aiohttp connector (its own
TCP pool, TLS context, credential resolution), and exiting it tears that
back down. Measured live (2026-08-19 latency audit): a presigned-URL
generation that touches the network *not at all* (pure local HMAC signing)
still cost 25-38ms because of this per-call setup/teardown; the same MinIO
GET cost 94.7ms with a fresh client vs 2.4ms with one reused (~39x).

aiobotocore's client wraps an aiohttp ClientSession, which is explicitly
designed for concurrent reuse across many in-flight requests (same
justification as the httpx.AsyncClient singleton fix applied to Omnius's
OpenRouterProvider this session) — so holding one client open for the
whole process and sharing it across concurrent requests is the correct,
supported usage, not a shortcut.

aioboto3's `.client()` is only usable as an async context manager, so
`AsyncExitStack` is used to enter it once at startup and keep it open
until `close_s3_client()` explicitly exits it at shutdown.
"""
from contextlib import AsyncExitStack
from typing import Optional

import aioboto3
from botocore.config import Config

from app.core.config import settings

_exit_stack: Optional[AsyncExitStack] = None
_client = None


async def init_s3_client():
    """Idempotent — safe to call multiple times (only the first does work)."""
    global _exit_stack, _client
    if _client is not None:
        return
    session = aioboto3.Session()
    stack = AsyncExitStack()
    _client = await stack.enter_async_context(
        session.client(
            "s3",
            endpoint_url=settings.STORAGE_ENDPOINT,
            aws_access_key_id=settings.STORAGE_ACCESS_KEY,
            aws_secret_access_key=settings.STORAGE_SECRET_KEY,
            config=Config(signature_version="s3v4"),
        )
    )
    _exit_stack = stack


async def close_s3_client():
    global _exit_stack, _client
    if _exit_stack is not None:
        await _exit_stack.aclose()
    _exit_stack = None
    _client = None


def get_s3_client():
    """Returns the shared client. Raises if init_s3_client() hasn't run —
    callers that can execute before app startup (rare — mainly tests) should
    call init_s3_client() themselves first."""
    if _client is None:
        raise RuntimeError(
            "S3 client not initialized — init_s3_client() must run at app "
            "startup (see app.core.events.on_startup) before any caller "
            "reaches get_s3_client()"
        )
    return _client
