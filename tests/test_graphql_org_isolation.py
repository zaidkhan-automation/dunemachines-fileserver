"""Cross-org isolation via the REAL GraphQL HTTP endpoint (/graphql),
not just resolver-level unit testing — the production remediation audit's
own explicit gap: the `assets` resolver was already confirmed (by reading
app/api/graphql/schema.py directly) to thread user["org_id"] into
asset_repo.list correctly, but no test exercised that through an actual
GraphQL request with a real token and real DB.

Uses httpx.AsyncClient with ASGITransport (in-process ASGI calls on the
SAME asyncio event loop as the test) rather than the sync TestClient —
TestClient drives the app through a separate portal thread with its own
event loop, which is exactly what tests/test_conversation_endpoints_...-
style comments across this codebase (and test_upload_completion_
ownership.py) document as incompatible with a real asyncpg/DB connection
created on pytest-asyncio's own loop. ASGITransport avoids that entirely,
so this test gets a genuine HTTP round trip AND real DB data in one place.
"""
import uuid

import httpx
import pytest

from main import app
from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.repositories.asset_repo import asset_repo
from app.models.asset import Asset, AssetStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _token_for_org(org_id: str) -> str:
    # Native, self-issued token (issuer defaults to "fileserver" per
    # create_access_token) — its own org_id claim is trusted directly,
    # matching test_bridged_token_trust.py's own
    # test_native_fileserver_token_trusts_its_own_claims precedent.
    return create_access_token({"sub": f"agent:test-{uuid.uuid4()}", "org_id": org_id, "roles": ["editor"]})


async def test_graphql_assets_query_never_returns_another_orgs_assets():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset_a = await asset_repo.create(db, {
            "organization_id": org_a,
            "created_by": str(uuid.uuid4()),
            "name": "org_a_confidential.txt",
            "asset_type": "document",
            "status": AssetStatus.READY,
        })
        await db.commit()

    query = "query { assets(limit: 50) { total assets { id name } } }"
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp_b = await client.post(
                "/graphql", json={"query": query},
                headers={"Authorization": f"Bearer {_token_for_org(org_b)}"},
            )
            resp_a = await client.post(
                "/graphql", json={"query": query},
                headers={"Authorization": f"Bearer {_token_for_org(org_a)}"},
            )

        assert resp_b.status_code == 200, resp_b.text
        assert resp_a.status_code == 200, resp_a.text

        names_b = {a["name"] for a in resp_b.json()["data"]["assets"]["assets"]}
        names_a = {a["name"] for a in resp_a.json()["data"]["assets"]["assets"]}

        assert "org_a_confidential.txt" not in names_b, "org B's GraphQL query must never see org A's asset"
        assert "org_a_confidential.txt" in names_a, "org A must see its own asset via the same query"
    finally:
        async with AsyncSessionLocal() as db:
            from sqlalchemy import delete
            await db.execute(delete(Asset).where(Asset.id == asset_a.id))
            await db.commit()


async def test_graphql_assets_query_unauthenticated_returns_empty_not_error():
    """No token at all — get_context leaves request.state.user None, the
    resolver's own `if not user: return AssetConnection(assets=[], ...)`
    must handle it gracefully (200 with an empty connection), never leak
    an unfiltered/default-org listing."""
    query = "query { assets(limit: 50) { total assets { id name } } }"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/graphql", json={"query": query})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["assets"]["total"] == 0
    assert body["data"]["assets"]["assets"] == []
