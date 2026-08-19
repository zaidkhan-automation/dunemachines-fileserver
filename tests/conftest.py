"""
Shared test fixtures.

The S3/MinIO client (app.core.s3_client) is now a process-lifetime
singleton, initialized in app.core.events.on_startup — real app runs
always go through that. Tests that call service-layer functions directly
(bypassing the FastAPI lifespan, by design — see
tests/test_presentation_links_service.py's docstring) never trigger that
startup hook, so without this fixture every test that reaches
get_s3_client() would fail with "S3 client not initialized" even though
storage itself is reachable and correctly configured.
"""
import pytest_asyncio

from app.core.s3_client import init_s3_client, close_s3_client


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _s3_client_lifecycle():
    await init_s3_client()
    yield
    await close_s3_client()
