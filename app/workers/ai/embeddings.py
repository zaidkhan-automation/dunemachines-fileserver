"""
AI Embeddings Worker
Triggered on ASSET_UPLOAD_COMPLETE event.
Extracts text, generates embeddings, stores in Qdrant.
"""
import logging
import asyncio
from typing import Optional
from app.events.consumers import on_event
from app.events.event_types import EventType

logger = logging.getLogger(__name__)


async def generate_embedding(text: str, model: str = "paraphrase-multilingual-mpnet-base-v2"):
    """Generate embedding vector for text — reuses cached singleton model."""
    from app.services.ai.embeddings import generate_embedding as _generate_embedding
    return await _generate_embedding(text)


def _extract_pdf_text_sync(content: bytes) -> str:
    """Blocking PDF parse — only ever called via run_in_executor, never
    directly from a coroutine (pypdf has no async API and a large/complex
    PDF can take seconds, which would stall every other request on this
    worker for the duration — same discipline as the dc74a25 live_terminal
    freeze-bug fix and simulations/engines/base.py's BLOCKING_STEP path)."""
    import pypdf
    import io
    reader = pypdf.PdfReader(io.BytesIO(content))
    text = " ".join(page.extract_text() or "" for page in reader.pages)
    return text[:10000]  # Cap at 10K chars


async def extract_text_from_asset(object_key: str, mime_type: str) -> Optional[str]:
    """Extract text from asset based on mime type."""
    try:
        from app.core.config import settings
        from app.core.s3_client import get_s3_client

        s3 = get_s3_client()
        response = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=object_key)
        content = await response["Body"].read()

        if mime_type == "application/pdf":
            try:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, _extract_pdf_text_sync, content)
            except Exception:
                return None
        elif mime_type.startswith("text/"):
            return content.decode("utf-8", errors="ignore")[:10000]
        else:
            return None
    except Exception as e:
        logger.error(f"Text extraction failed: {e}")
        return None


async def store_embedding_qdrant(asset_id: str, org_id: str, vector: list, text: str, asset_type: str = "file", name: str = ""):
    """Store embedding in Qdrant via indexer."""
    try:
        from app.services.search.indexer import index_asset
        success = await index_asset(
            asset_id=asset_id,
            org_id=org_id,
            text=text,
            asset_type=asset_type,
            name=name,
        )
        if success:
            logger.info(f"Embedding stored in Qdrant for asset {asset_id}")
        else:
            logger.error(f"Qdrant storage failed for asset {asset_id}")
    except Exception as e:
        logger.error(f"Qdrant storage failed: {e}")


@on_event(EventType.ASSET_UPLOAD_COMPLETE)
async def handle_upload_complete(payload: dict):
    """Process asset after upload — extract text, generate embedding."""
    asset_id = payload.get("asset_id")
    org_id = payload.get("org_id")
    object_key = payload.get("object_key")

    if not all([asset_id, org_id, object_key]):
        return

    # Idempotency check — Redis lock prevents duplicate processing across workers
    try:
        from app.core.redis import get_redis
        redis = await get_redis()
        lock_key = f"embedding:lock:{asset_id}"
        acquired = await redis.set(lock_key, "1", nx=True, ex=300)
        if not acquired:
            logger.info(f"Asset {asset_id} already being processed — skipping")
            return
    except Exception as e:
        logger.warning(f"Redis lock failed — proceeding anyway: {e}")

    logger.info(f"Processing embeddings for asset {asset_id}")

    # Get asset mime type from DB
    try:
        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo
        from app.models.asset import AssetStatus

        async with AsyncSessionLocal() as db:
            asset = await asset_repo.get_by_id(db, asset_id, org_id)
            if not asset:
                return

            mime_type = asset.mime_type or "application/octet-stream"

            # Extract text
            text = await extract_text_from_asset(object_key, mime_type)
            if not text:
                # Use filename as fallback
                text = asset.name

            # Generate embedding — reuse cached singleton model (avoids
            # re-downloading/re-verifying against HuggingFace on every upload,
            # which was crashing the worker process under load)
            from app.services.ai.embeddings import generate_embedding as _generate_embedding
            vector = await _generate_embedding(text[:512])

            if vector:
                await store_embedding_qdrant(
                    asset_id=asset_id,
                    org_id=org_id,
                    vector=vector,
                    text=text,
                    asset_type=asset.asset_type,
                    name=asset.name
                )

            # Re-check the asset still exists (not deleted while we were
            # processing — embedding generation can take several seconds,
            # asset may have been deleted mid-flight).
            still_exists = await asset_repo.get_by_id(db, asset_id, org_id)
            if not still_exists:
                logger.info(f"Asset {asset_id} was deleted during processing — skipping status update")
                return

            # Update asset status to ready
            await asset_repo.update_status(db, asset_id, AssetStatus.READY)
            await db.commit()

            from app.events.producers import publish_event
            await publish_event(EventType.ASSET_UPDATED, {
                "asset_id": asset_id,
                "org_id": org_id,
                "status": "ready",
            })

            # Request summary generation for text-bearing assets
            if text and len(text.strip()) > 50:
                await publish_event(EventType.AI_SUMMARY_REQUESTED, {
                    "asset_id": asset_id,
                    "org_id": org_id,
                    "text": text,
                    "filename": asset.name,
                })

            logger.info(f"Asset {asset_id} processed successfully ✅")

    except Exception as e:
        logger.error(f"Embedding worker failed for {asset_id}: {e}")
