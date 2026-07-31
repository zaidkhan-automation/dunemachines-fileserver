"""
AI Summary Worker
Generates summaries for documents using LLM.
"""
import logging
from app.events.consumers import on_event
from app.events.event_types import EventType

logger = logging.getLogger(__name__)


async def generate_summary(text: str, filename: str) -> str:
    """Generate a concise summary using available LLM."""
    try:
        import httpx
        from app.core.config import settings

        # Use Mistral for summarization
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {getattr(settings, 'MISTRAL_API_KEY', '')}"},
                json={
                    "model": "mistral-small-latest",
                    "messages": [{
                        "role": "user",
                        "content": f"Summarize this document in 2-3 sentences. Filename: {filename}\n\nContent: {text[:2000]}"
                    }],
                    "max_tokens": 150,
                }
            )
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return f"Document: {filename}"


@on_event(EventType.AI_SUMMARY_REQUESTED)
async def handle_summary_request(payload: dict):
    """Generate and store summary for an asset."""
    asset_id = payload.get("asset_id")
    org_id = payload.get("org_id")
    text = payload.get("text", "")
    filename = payload.get("filename", "")

    if not asset_id:
        return

    try:
        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo
        from sqlalchemy import update
        from app.models.asset import Asset

        summary = await generate_summary(text, filename)

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Asset).where(Asset.id == asset_id).values(summary=summary)
            )
            await db.commit()
            logger.info(f"Summary stored for asset {asset_id}")

    except Exception as e:
        logger.error(f"Summary worker failed: {e}")
