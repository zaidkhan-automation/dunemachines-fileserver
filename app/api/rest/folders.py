"""
Folder endpoints — folders are Asset rows with asset_type="folder".
Uses the same Asset/parent_id tree as regular files.
"""
import uuid as _uuid
from fastapi import APIRouter, Query, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.repositories.asset_repo import asset_repo
from app.models.asset import AssetType, AssetStatus, SourceType
from app.events.producers import publish_event
from app.events.event_types import EventType

router = APIRouter(prefix="/folders", tags=["folders"])

DEFAULT_ORG = "00000000-0000-0000-0000-000000000001"
DEFAULT_USER = "00000000-0000-0000-0000-000000000000"


def _safe_uuid(val: Optional[str], fallback: str) -> str:
    if not val:
        return fallback
    try:
        _uuid.UUID(str(val))
        return val
    except (ValueError, AttributeError):
        return fallback


def _asset_to_folder_dict(asset) -> Dict:
    return {
        "id": str(asset.id),
        "name": asset.name,
        "parent_id": str(asset.parent_id) if asset.parent_id else None,
        "project_id": str(asset.project_id) if asset.project_id else None,
        "asset_type": asset.asset_type,
        "status": asset.status,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
    }


class CreateFolderRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=512)
    parent_id: Optional[str] = None
    project_id: Optional[str] = None


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_folder(
    req: CreateFolderRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Create a new folder (an Asset row with asset_type=folder)."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)
    created_by = _safe_uuid(user.get("user_id"), DEFAULT_USER)

    # If parent_id given, verify it exists and is actually a folder
    if req.parent_id:
        parent = await asset_repo.get_by_id(db, req.parent_id, org_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Parent folder not found")
        if parent.asset_type != AssetType.FOLDER:
            raise HTTPException(status_code=400, detail="parent_id does not refer to a folder")

    folder = await asset_repo.create(db, {
        "organization_id": org_id,
        "project_id": req.project_id,
        "parent_id": req.parent_id,
        "created_by": created_by,
        "name": req.name,
        "asset_type": AssetType.FOLDER,
        "source_type": SourceType.UPLOAD,
        "status": AssetStatus.READY,
    })

    await publish_event(EventType.ASSET_CREATED, {
        "asset_id": str(folder.id),
        "org_id": org_id,
        "name": folder.name,
        "asset_type": folder.asset_type,
        "parent_id": str(folder.parent_id) if folder.parent_id else None,
        "status": folder.status,
    })

    return _asset_to_folder_dict(folder)


@router.get("/{folder_id}/contents")
async def list_folder_contents(
    folder_id: str,
    asset_type: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """List contents (files + subfolders) of a folder."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)

    folder = await asset_repo.get_by_id(db, folder_id, org_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    items, total = await asset_repo.list(
        db, org_id=org_id, parent_id=folder_id,
        asset_type=asset_type, limit=limit, offset=offset,
    )

    return {
        "folder_id": folder_id,
        "folder_name": folder.name,
        "items": [_asset_to_folder_dict(a) for a in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: str,
    recursive: bool = False,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Delete a folder. recursive=True also soft-deletes all contents."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)

    folder = await asset_repo.get_by_id(db, folder_id, org_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")

    deleted_count = 0
    if recursive:
        items, _ = await asset_repo.list(db, org_id=org_id, parent_id=folder_id, limit=1000)
        for item in items:
            await asset_repo.soft_delete(db, str(item.id), org_id)
            deleted_count += 1
    else:
        items, total = await asset_repo.list(db, org_id=org_id, parent_id=folder_id, limit=1)
        if total > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Folder is not empty ({total} items). Use recursive=true to delete anyway.",
            )

    await asset_repo.soft_delete(db, folder_id, org_id)
    deleted_count += 1

    await publish_event(EventType.ASSET_DELETED, {
        "asset_id": folder_id,
        "org_id": org_id,
        "name": folder.name,
        "parent_id": str(folder.parent_id) if folder.parent_id else None,
        "recursive": recursive,
        "items_deleted": deleted_count,
    })

    return {"folder_id": folder_id, "deleted": True, "recursive": recursive, "items_deleted": deleted_count}
