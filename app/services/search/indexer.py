"""
Search indexer — stores/deletes asset embeddings in Qdrant.
"""
import logging
import uuid
from typing import Optional, List
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance, Filter, FieldCondition, MatchValue
from app.core.config import settings
from app.services.ai.embeddings import generate_embedding

logger = logging.getLogger(__name__)

VECTOR_SIZE = 768
COLLECTION_PREFIX = "fileserver_assets"

_client: Optional[AsyncQdrantClient] = None


def get_qdrant() -> AsyncQdrantClient:
    global _client
    if _client is None:
        if not settings.QDRANT_API_KEY and not _looks_private(settings.QDRANT_URL):
            # Not a hard fail — this exact (public-looking IP, no API key)
            # configuration may already be firewalled at the network
            # level, which this process has no way to observe. Loud
            # warning instead of raising, so a legitimately-firewalled
            # deployment doesn't get broken by this check.
            logger.warning(
                f"QDRANT_URL ({settings.QDRANT_URL}) is not a private/localhost address and "
                "QDRANT_API_KEY is unset — confirm this is firewalled at the network level; "
                "otherwise every org's asset embeddings are readable/writable unauthenticated."
            )
        _client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
            check_compatibility=False,
        )
    return _client


def _looks_private(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _collection_name(org_id: str) -> str:
    return f"{COLLECTION_PREFIX}_{org_id[:8]}"


def _point_id(asset_id: str) -> str:
    """Use UUID string directly as Qdrant point ID."""
    return str(uuid.UUID(asset_id))


async def ensure_collection(org_id: str):
    """Create Qdrant collection if not exists."""
    collection = _collection_name(org_id)
    try:
        client = get_qdrant()
        existing = [c.name for c in (await client.get_collections()).collections]
        if collection not in existing:
            await client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
            )
            logger.info(f"Qdrant collection created: {collection}")
    except Exception as e:
        logger.error(f"Collection creation failed: {e}")


async def index_asset(
    asset_id: str,
    org_id: str,
    text: str,
    asset_type: str,
    name: str,
    project_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
):
    """Index asset into Qdrant."""
    try:
        vector = await generate_embedding(text or name)
        if not vector:
            logger.warning(f"No embedding for asset {asset_id}")
            return False

        await ensure_collection(org_id)
        client = get_qdrant()

        await client.upsert(
            collection_name=_collection_name(org_id),
            points=[PointStruct(
                id=_point_id(asset_id),
                vector=vector,
                payload={
                    "asset_id": asset_id,
                    "org_id": org_id,
                    "asset_type": asset_type,
                    "name": name,
                    "project_id": project_id or "",
                    "tags": tags or [],
                    "text_preview": (text or "")[:300],
                }
            )]
        )
        logger.info(f"Asset {asset_id} indexed in Qdrant ✅")
        return True
    except Exception as e:
        logger.error(f"Indexing failed for asset {asset_id}: {e}")
        return False


async def delete_from_index(asset_id: str, org_id: str):
    """Remove asset from Qdrant index."""
    try:
        client = get_qdrant()
        await client.delete(
            collection_name=_collection_name(org_id),
            points_selector=[_point_id(asset_id)]
        )
        logger.info(f"Asset {asset_id} removed from index")
    except Exception as e:
        logger.error(f"Index deletion failed for asset {asset_id}: {e}")


async def semantic_search(
    org_id: str,
    query: str,
    asset_types: Optional[List[str]] = None,
    project_id: Optional[str] = None,
    limit: int = 20,
    score_threshold: float = 0.3,
) -> List[dict]:
    """Search assets by semantic similarity."""
    try:
        vector = await generate_embedding(query)
        if not vector:
            return []

        await ensure_collection(org_id)
        client = get_qdrant()

        # Build filter
        must_conditions = []
        if asset_types:
            must_conditions.append(
                FieldCondition(key="asset_type", match=MatchValue(value=asset_types[0]))
                if len(asset_types) == 1
                else FieldCondition(key="asset_type", match=MatchValue(any=asset_types))
            )
        if project_id:
            must_conditions.append(
                FieldCondition(key="project_id", match=MatchValue(value=project_id))
            )

        qdrant_filter = Filter(must=must_conditions) if must_conditions else None

        results = await client.query_points(
            collection_name=_collection_name(org_id),
            query=vector,
            query_filter=qdrant_filter,
            limit=limit,
            score_threshold=score_threshold,
        )

        # Isolation here is structural — each org has its own Qdrant
        # collection (_collection_name(org_id)), not a shared index
        # filtered by an org_id field — so a cross-org result shouldn't
        # be reachable at all. This assertion is a defense-in-depth
        # backstop, not the actual isolation mechanism: it'd only catch a
        # future bug (e.g. a collection name collision or reuse) rather
        # than something expected to fire under normal operation.
        points = []
        for r in results.points:
            point_org = r.payload.get("org_id")
            if point_org is not None and point_org != org_id:
                logger.error(
                    f"Cross-org search result dropped: point org_id={point_org} "
                    f"!= requested org_id={org_id} in collection {_collection_name(org_id)}"
                )
                continue
            points.append(r)

        return [
            {
                "asset_id": r.payload.get("asset_id"),
                "score": r.score,
                "asset_type": r.payload.get("asset_type"),
                "name": r.payload.get("name"),
                "project_id": r.payload.get("project_id"),
                "tags": r.payload.get("tags", []),
                "text_preview": r.payload.get("text_preview", ""),
            }
            for r in points
        ]
    except Exception as e:
        logger.error(f"Semantic search failed: {e}")
        return []
