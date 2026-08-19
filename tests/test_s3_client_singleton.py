"""
Regression tests for the aioboto3 client-reuse fix in app/core/s3_client.py.

Before this fix, every S3 caller (presentation_service, upload_service,
files.py's delete path, the embeddings/thumbnails workers) did
`aioboto3.Session()` + a fresh `async with session.client(...)` on every
single invocation. Measured live (2026-08-19 latency audit): a presigned
URL generation that touches the network not at all still cost 25-38ms
because of that per-call setup/teardown; the same MinIO GET cost 94.7ms
fresh vs 2.4ms reused (~39x). These tests assert the actual property that
matters — one client object is created and reused across calls, real
MinIO operations succeed through it, and shutdown tears it down cleanly —
not just that individual calls still work (already covered by
test_presentation_links_service.py and test_presentation_links_router.py,
both unaffected by this change per the full-suite run).

All tests here are pinned to loop_scope="session" — matching
conftest.py's `_s3_client_lifecycle` fixture, which holds the real
singleton open for the whole test run on the session event loop. A test
that closed/reinitialized the client on a function-scoped loop instead
would bind aiohttp's connector to a loop that's torn down the moment that
one test function ends, breaking every test after it — a test-harness
pitfall, not something the singleton itself does wrong.
"""
import pytest

from app.core.s3_client import init_s3_client, close_s3_client, get_s3_client


@pytest.mark.asyncio(loop_scope="session")
async def test_get_s3_client_raises_before_init():
    await close_s3_client()
    try:
        with pytest.raises(RuntimeError, match="not initialized"):
            get_s3_client()
    finally:
        await init_s3_client()  # restore — conftest's fixture owns this for the rest of the session


@pytest.mark.asyncio(loop_scope="session")
async def test_init_creates_a_reusable_client():
    client_a = get_s3_client()
    client_b = get_s3_client()
    assert client_a is client_b


@pytest.mark.asyncio(loop_scope="session")
async def test_init_is_idempotent():
    first = get_s3_client()
    await init_s3_client()  # second call must be a no-op, not a new client
    second = get_s3_client()
    assert first is second


@pytest.mark.asyncio(loop_scope="session")
async def test_close_clears_the_client():
    assert get_s3_client() is not None
    await close_s3_client()
    try:
        with pytest.raises(RuntimeError):
            get_s3_client()
    finally:
        await init_s3_client()  # restore — conftest's fixture owns this for the rest of the session


@pytest.mark.asyncio(loop_scope="session")
async def test_reused_client_performs_a_real_minio_round_trip():
    """Not just "a client object exists" — it must actually work against
    real MinIO, proving the held-open connector wasn't left in a broken
    state by being shared across calls."""
    import uuid
    from app.core.config import settings

    s3 = get_s3_client()
    key = f"_test/s3_singleton/{uuid.uuid4()}.txt"
    try:
        await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=key, Body=b"singleton works")
        # Second call, same client instance, proves it's genuinely reusable
        resp = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=key)
        body = await resp["Body"].read()
        assert body == b"singleton works"
    finally:
        await s3.delete_object(Bucket=settings.STORAGE_BUCKET, Key=key)
