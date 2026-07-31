"""
Search service — fulltext, semantic, hybrid.
"""
import logging
from typing import List, Optional, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class SearchMode(str, Enum):
    FULLTEXT = "fulltext"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


class SearchService:
    async def search(
        self,
        org_id: str,
        query: str,
        mode: SearchMode = SearchMode.HYBRID,
        asset_types: Optional[List[str]] = None,
        project_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        filters: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if mode == SearchMode.FULLTEXT:
            return await self._fulltext_search(org_id, query, asset_types, limit, offset)
        elif mode == SearchMode.SEMANTIC:
            return await self._semantic_search(org_id, query, asset_types, project_id, limit)
        else:
            return await self._hybrid_search(org_id, query, asset_types, project_id, limit, offset)

    async def _fulltext_search(self, org_id, query, asset_types, limit, offset):
        from app.repositories.asset_repo import asset_repo
        from app.core.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            assets, total = await asset_repo.search_fulltext(
                db, org_id=org_id, query=query,
                asset_types=asset_types, limit=limit, offset=offset
            )
        return {
            "results": [self._asset_to_dict(a) for a in assets],
            "total": total,
            "mode": "fulltext"
        }

    async def _semantic_search(self, org_id, query, asset_types, project_id, limit):
        from app.services.search.indexer import semantic_search
        from app.repositories.asset_repo import asset_repo
        from app.core.database import AsyncSessionLocal
        import uuid as _uuid

        # Normalize org_id — fallback for Duniverse users
        try:
            _uuid.UUID(str(org_id))
        except (ValueError, AttributeError):
            org_id = "00000000-0000-0000-0000-000000000001"

        hits = await semantic_search(
            org_id=org_id,
            query=query,
            asset_types=asset_types,
            project_id=project_id,
            limit=limit,
        )

        if not hits:
            return {"results": [], "total": 0, "mode": "semantic"}

        # Fetch full asset data from DB
        asset_ids = [h["asset_id"] for h in hits if h["asset_id"]]
        score_map = {h["asset_id"]: h["score"] for h in hits}

        async with AsyncSessionLocal() as db:
            results = []
            for asset_id in asset_ids:
                asset = await asset_repo.get_by_id(db, asset_id, org_id)
                if asset:
                    d = self._asset_to_dict(asset)
                    d["score"] = round(score_map.get(asset_id, 0), 4)
                    results.append(d)

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return {"results": results, "total": len(results), "mode": "semantic"}

    async def _hybrid_search(self, org_id, query, asset_types, project_id, limit, offset):
        """RRF fusion — fulltext + semantic results combined."""
        import asyncio
        ft_task = asyncio.create_task(
            self._fulltext_search(org_id, query, asset_types, limit, offset)
        )
        sem_task = asyncio.create_task(
            self._semantic_search(org_id, query, asset_types, project_id, limit)
        )
        ft_result, sem_result = await asyncio.gather(ft_task, sem_task)

        # RRF scoring
        K = 60
        scores = {}
        asset_map = {}

        for rank, item in enumerate(ft_result["results"]):
            aid = item["id"]
            scores[aid] = scores.get(aid, 0) + 1 / (K + rank + 1)
            asset_map[aid] = item

        for rank, item in enumerate(sem_result["results"]):
            aid = item["id"]
            scores[aid] = scores.get(aid, 0) + 1 / (K + rank + 1)
            asset_map[aid] = item

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for asset_id, score in ranked[:limit]:
            item = asset_map[asset_id].copy()
            item["score"] = round(score, 6)
            results.append(item)

        return {"results": results, "total": len(results), "mode": "hybrid"}

    def _asset_to_dict(self, asset) -> Dict:
        return {
            "id": str(asset.id),
            "name": asset.name,
            "asset_type": asset.asset_type,
            "source_type": asset.source_type,
            "status": asset.status,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "summary": asset.summary,
            "tags": asset.tags or [],
            "extra_data": asset.extra_data or {},
            "created_at": asset.created_at.isoformat(),
            "updated_at": asset.updated_at.isoformat(),
        }

    async def index_asset(self, asset_id: str, org_id: str):
        """Index asset after upload complete."""
        from app.services.search.indexer import index_asset
        from app.repositories.asset_repo import asset_repo
        from app.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            asset = await asset_repo.get_by_id(db, asset_id, org_id)
            if not asset:
                return
            text = asset.summary or asset.name
            await index_asset(
                asset_id=str(asset.id),
                org_id=org_id,
                text=text,
                asset_type=asset.asset_type,
                name=asset.name,
                project_id=str(asset.project_id) if asset.project_id else None,
                tags=asset.tags or [],
            )

    async def delete_from_index(self, asset_id: str, org_id: str):
        from app.services.search.indexer import delete_from_index
        await delete_from_index(asset_id, org_id)


search_service = SearchService()
