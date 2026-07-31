"""
Thumbnail Worker
Generates thumbnails for images and PDFs.
"""
import logging
import io
from app.events.consumers import on_event
from app.events.event_types import EventType

logger = logging.getLogger(__name__)


async def generate_thumbnail(content: bytes, mime_type: str) -> bytes:
    """Generate thumbnail from file content."""
    try:
        from PIL import Image
        if mime_type.startswith("image/"):
            img = Image.open(io.BytesIO(content))
            # Convert RGBA/P/LA modes to RGB — JPEG doesn't support alpha channel
            if img.mode in ("RGBA", "P", "LA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((256, 256))
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=85)
            return output.getvalue()
        return b""
    except Exception as e:
        logger.error(f"Thumbnail generation failed: {e}")
        return b""


async def upload_thumbnail(asset_id: str, thumbnail: bytes):
    """Upload thumbnail to MinIO."""
    try:
        import aioboto3
        from app.core.config import settings

        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=settings.STORAGE_ENDPOINT,
            aws_access_key_id=settings.STORAGE_ACCESS_KEY,
            aws_secret_access_key=settings.STORAGE_SECRET_KEY,
        ) as s3:
            await s3.put_object(
                Bucket=settings.STORAGE_BUCKET,
                Key=f"thumbnails/{asset_id}.jpg",
                Body=thumbnail,
                ContentType="image/jpeg",
            )
        logger.info(f"Thumbnail uploaded for asset {asset_id}")
    except Exception as e:
        logger.error(f"Thumbnail upload failed: {e}")


@on_event(EventType.ASSET_UPLOAD_COMPLETE)
async def handle_thumbnail_generation(payload: dict):
    """Generate thumbnail after upload — only for images."""
    asset_id = payload.get("asset_id")
    org_id = payload.get("org_id")
    object_key = payload.get("object_key")

    if not all([asset_id, org_id, object_key]):
        return

    try:
        from app.core.database import AsyncSessionLocal
        from app.repositories.asset_repo import asset_repo

        async with AsyncSessionLocal() as db:
            asset = await asset_repo.get_by_id(db, asset_id, org_id)
            if not asset or not asset.mime_type or not asset.mime_type.startswith("image/"):
                return

            import aioboto3
            from app.core.config import settings
            session = aioboto3.Session()
            async with session.client(
                "s3",
                endpoint_url=settings.STORAGE_ENDPOINT,
                aws_access_key_id=settings.STORAGE_ACCESS_KEY,
                aws_secret_access_key=settings.STORAGE_SECRET_KEY,
            ) as s3:
                response = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=object_key)
                content = await response["Body"].read()

            thumbnail = await generate_thumbnail(content, asset.mime_type)
            if thumbnail:
                await upload_thumbnail(asset_id, thumbnail)
                logger.info(f"Thumbnail generated for asset {asset_id}")

    except Exception as e:
        logger.error(f"Thumbnail worker failed for {asset_id}: {e}")
