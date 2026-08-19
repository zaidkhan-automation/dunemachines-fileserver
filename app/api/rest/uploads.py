"""
Upload endpoints — signed URL based upload flow.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any
from app.services.uploads.upload_service import upload_service
from app.core.database import get_db
from app.core.config import settings
from app.core.security import get_current_user
from app.core.rate_limit import limiter
from app.services.permissions.rbac import require_permission
from app.repositories.asset_repo import asset_repo
from app.events.producers import publish_upload_complete, publish_event
from app.events.event_types import EventType
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/uploads", tags=["uploads"])


class InitUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    mime_type: str = Field(..., min_length=1, max_length=256)
    size_bytes: int = Field(..., gt=0, le=settings.MAX_FILE_SIZE_MB * 1024 * 1024)
    project_id: Optional[str] = None
    parent_id: Optional[str] = None
    asset_type: Optional[str] = None
    extra_data: Optional[Dict[str, Any]] = None

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, v: str) -> str:
        # Normalize before comparing against the allowlist — a client can
        # send "TEXT/PLAIN; charset=utf-8", semantically identical to
        # "text/plain" but a literal-string allowlist match would reject
        # it. Returns the normalized form so downstream storage/DB use is
        # consistent too, not just the allowlist check.
        normalized = v.strip().lower().split(";", 1)[0].strip()
        # ALLOWED_MIME_TYPES defaults to [] — empty means "not configured,
        # no restriction", preserving today's fully-permissive behavior
        # until an operator actually opts into an allowlist.
        if settings.ALLOWED_MIME_TYPES and normalized not in settings.ALLOWED_MIME_TYPES:
            raise ValueError(f"mime_type '{v}' is not in the allowed list")
        return normalized

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v: str) -> str:
        # filename flows unsanitized into the S3 object key
        # (orgs/{org}/assets/{id}/{filename}) and into a literal
        # Content-Disposition response header on download — strip path
        # separators/traversal segments and control/quote characters so
        # neither of those two contexts can be manipulated by a crafted
        # filename. Keep just the basename, matching what every real
        # client actually needs (a display name), not a path.
        import re

        base = v.replace("\\", "/").rsplit("/", 1)[-1].strip()
        base = base.replace("..", "")
        base = re.sub(r'[\x00-\x1f\x7f"]', "", base)
        base = base.strip()
        if not base:
            raise ValueError("filename is empty after sanitization")
        return base


class CompleteUploadRequest(BaseModel):
    object_key: str
    checksum: Optional[str] = None


@router.post("/init", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def init_upload(
    request: Request,
    req: InitUploadRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(require_permission("assets", "upload")),
):
    """
    Step 1 of upload flow.
    Returns signed URL — client uploads directly to MinIO.
    """
    try:
        result = await upload_service.init_upload(
            org_id=user["org_id"],
            project_id=req.project_id,
            user_id=user["user_id"],
            filename=req.filename,
            mime_type=req.mime_type,
            size_bytes=req.size_bytes,
            asset_type=req.asset_type,
            metadata=req.extra_data,
        )

        # Save asset record to DB
        # Handle agent/non-UUID user IDs
        import uuid as _uuid
        user_id = user["user_id"]
        try:
            _uuid.UUID(str(user_id))
        except (ValueError, AttributeError):
            user_id = "00000000-0000-0000-0000-000000000000"

        # org_id fallback — Duniverse users get default org UUID
        org_id = user["org_id"]
        try:
            _uuid.UUID(str(org_id))
        except (ValueError, AttributeError):
            org_id = "00000000-0000-0000-0000-000000000001"

        asset = await asset_repo.create(db, {
            "id": result["upload_id"],
            "organization_id": org_id,
            "project_id": req.project_id,
            "parent_id": req.parent_id,
            "created_by": user_id,
            "name": req.filename,
            "asset_type": result["asset"]["asset_type"],
            "mime_type": req.mime_type,
            "size_bytes": req.size_bytes,
            # blob_ref intentionally omitted — stays NULL until /complete
            # verifies the object actually landed in storage. Setting it
            # here (at init, before the client's PUT even starts) made
            # download-url's `if not asset.blob_ref` check pass for an
            # object that doesn't exist yet, returning a 404-from-storage
            # that reads as "empty content" to callers.
            "blob_bucket": "dunemachines-files",
            "extra_data": req.extra_data or {},
        })

        result["asset"]["id"] = str(asset.id)
        result["upload_id"] = str(asset.id)

        await publish_event(EventType.ASSET_CREATED, {
            "asset_id": str(asset.id),
            "org_id": org_id,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "parent_id": str(asset.parent_id) if asset.parent_id else None,
            "status": asset.status,
        })

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{upload_id}/complete")
@limiter.limit("5/minute")
async def complete_upload(
    request: Request,
    upload_id: str,
    req: CompleteUploadRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """
    Step 3 of upload flow — called after client finishes uploading to MinIO.
    Triggers AI processing workers.
    """
    import uuid as _uuid
    _org = user["org_id"]
    try:
        _uuid.UUID(str(_org))
    except (ValueError, AttributeError):
        _org = "00000000-0000-0000-0000-000000000001"

    asset = await asset_repo.get_by_id(db, upload_id, _org)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Object must actually exist in storage before we mark this asset
    # complete — blob_ref is no longer set at /init, so this is the one
    # place that gates a "ready" asset on a verified upload. Without this,
    # a client that never finished its PUT (or PUT'd to the wrong key)
    # could still call /complete and leave a permanently-empty asset that
    # looks saved.
    try:
        await upload_service.complete_upload(upload_id, req.object_key, req.checksum)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Verify actual object checksum matches what client claims
    if req.checksum:
        try:
            import hashlib
            from app.core.config import settings
            from app.core.s3_client import get_s3_client
            s3 = get_s3_client()
            response = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=req.object_key)
            content = await response["Body"].read()
            actual_checksum = hashlib.sha256(content).hexdigest()
            if actual_checksum != req.checksum:
                raise HTTPException(
                    status_code=422,
                    detail=f"Checksum mismatch — file may be corrupted during upload"
                )
        except HTTPException:
            raise
        except Exception as e:
            # If we can't verify (e.g. object not found yet), log but don't block
            import logging
            logging.getLogger(__name__).warning(f"Checksum verification skipped: {e}")

    # Update blob ref and status
    await asset_repo.update_blob(
        db,
        upload_id,
        blob_ref=req.object_key,
        blob_bucket="dunemachines-files",
        checksum=req.checksum,
    )

    # Publish event — triggers AI workers
    await publish_upload_complete(
        asset_id=upload_id,
        org_id=user["org_id"],
        object_key=req.object_key,
    )

    return {
        "asset_id": upload_id,
        "status": "processing",
        "message": "Upload complete. AI processing started.",
    }


@router.get("/{upload_id}/download-url")
@limiter.limit("50/minute")
async def get_download_url(
    request: Request,
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Get presigned download URL."""
    import uuid as _uuid
    _org = user["org_id"]
    try:
        _uuid.UUID(str(_org))
    except (ValueError, AttributeError):
        _org = "00000000-0000-0000-0000-000000000001"
    asset = await asset_repo.get_by_id(db, upload_id, _org)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not asset.blob_ref:
        raise HTTPException(status_code=400, detail="Asset has no file attached")

    url = await upload_service.generate_presigned_get(
        object_key=asset.blob_ref,
        filename=asset.name,
    )
    return {"download_url": url, "expires_in": 3600, "filename": asset.name}
