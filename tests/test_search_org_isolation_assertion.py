"""
Finding #12: real isolation is structural (one Qdrant collection per
org, not a shared collection filtered by org_id), so a cross-org result
shouldn't be reachable at all — this defense-in-depth assertion is a
backstop for a future bug (e.g. collection reuse), not the actual
isolation mechanism.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.search.indexer import semantic_search


def _point(asset_id, org_id, score=0.9):
    return SimpleNamespace(
        score=score,
        payload={"asset_id": asset_id, "org_id": org_id, "asset_type": "file", "name": "x", "project_id": "", "tags": [], "text_preview": ""},
    )


@pytest.mark.asyncio
async def test_mismatched_org_id_result_dropped():
    fake_client = AsyncMock()
    fake_client.get_collections = AsyncMock(return_value=SimpleNamespace(collections=[SimpleNamespace(name="fileserver_assets_orgA")]))
    fake_client.query_points = AsyncMock(return_value=SimpleNamespace(points=[
        _point("asset-own", "org-A"),
        _point("asset-leaked", "org-B"),  # shouldn't survive the filter
    ]))

    with patch("app.services.search.indexer.get_qdrant", return_value=fake_client), \
         patch("app.services.search.indexer.generate_embedding", AsyncMock(return_value=[0.1] * 768)):
        results = await semantic_search(org_id="org-A", query="anything")

    ids = [r["asset_id"] for r in results]
    assert "asset-own" in ids
    assert "asset-leaked" not in ids


@pytest.mark.asyncio
async def test_all_matching_org_results_kept():
    fake_client = AsyncMock()
    fake_client.get_collections = AsyncMock(return_value=SimpleNamespace(collections=[SimpleNamespace(name="fileserver_assets_orgA")]))
    fake_client.query_points = AsyncMock(return_value=SimpleNamespace(points=[
        _point("asset-1", "org-A"),
        _point("asset-2", "org-A"),
    ]))

    with patch("app.services.search.indexer.get_qdrant", return_value=fake_client), \
         patch("app.services.search.indexer.generate_embedding", AsyncMock(return_value=[0.1] * 768)):
        results = await semantic_search(org_id="org-A", query="anything")

    assert len(results) == 2
