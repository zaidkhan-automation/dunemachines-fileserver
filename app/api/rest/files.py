"""
Asset CRUD endpoints.
"""
from fastapi import APIRouter, HTTPException, Query, Depends, BackgroundTasks, Request
from typing import Optional, List, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user
from app.services.permissions.rbac import require_permission
from app.repositories.asset_repo import asset_repo
from app.services.search.search_service import search_service, SearchMode
from app.events.producers import publish_event
from app.events.event_types import EventType
from pydantic import BaseModel as _BaseModel

router = APIRouter(prefix="/assets", tags=["assets"])


def _asset_to_dict(asset) -> Dict:
    return {
        "id": str(asset.id),
        "name": asset.name,
        "asset_type": asset.asset_type,
        "source_type": asset.source_type,
        "status": asset.status,
        "parent_id": str(asset.parent_id) if asset.parent_id else None,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "summary": asset.summary,
        "tags": asset.tags or [],
        "extra_data": asset.extra_data or {},
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
    }


@router.get("/search")
async def search_assets(
    request: Request,
    q: str = Query(..., min_length=1),
    mode: str = Query(default="hybrid"),
    asset_types: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Search assets — fulltext, semantic, or hybrid."""
    import uuid as _uuid
    org_id = user["org_id"]
    try:
        _uuid.UUID(str(org_id))
    except (ValueError, AttributeError):
        org_id = "00000000-0000-0000-0000-000000000001"

    result = await search_service.search(
        org_id=org_id,
        query=q,
        mode=SearchMode(mode) if mode in [m.value for m in SearchMode] else SearchMode.HYBRID,
        asset_types=asset_types,
        limit=limit,
        offset=offset,
    )
    result["query"] = q
    return result


@router.get("/")
async def list_assets(
    project_id: Optional[str] = None,
    asset_type: Optional[str] = None,
    parent_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """List assets with filtering."""
    assets, total = await asset_repo.list(
        db,
        org_id=user["org_id"],
        project_id=project_id,
        asset_type=asset_type,
        parent_id=parent_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "assets": [_asset_to_dict(a) for a in assets],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


class ContentEventRequest(_BaseModel):
    event_type: str  # "write" | "edit"
    content_preview: Optional[str] = None  # truncated preview, for write
    diff: Optional[str] = None             # unified diff, for edit
    path: Optional[str] = None


@router.post("/{asset_id}/content-event")
async def report_content_event(
    asset_id: str,
    req: ContentEventRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """
    Report a live content write/edit for an asset — broadcasts
    to connected WS clients in real time. Does not store the
    content itself (asset's blob_ref is the source of truth);
    this is purely for live streaming UX.
    """
    asset = await asset_repo.get_by_id(db, asset_id, user["org_id"])
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    event_type = EventType.FILE_CONTENT_EDIT if req.event_type == "edit" else EventType.FILE_CONTENT_WRITE

    payload = {
        "asset_id": asset_id,
        "org_id": user["org_id"],
        "name": asset.name,
        "path": req.path,
    }
    if req.content_preview is not None:
        payload["content_preview"] = req.content_preview[:2000]
    if req.diff is not None:
        payload["diff"] = req.diff[:5000]

    await publish_event(event_type, payload)

    return {"reported": True, "asset_id": asset_id, "event_type": req.event_type}


@router.get("/tree")
async def get_full_tree(
    root_id: Optional[str] = Query(default=None, description="Start from this folder, or omit for org root"),
    project_id: Optional[str] = None,
    max_depth: int = Query(default=10, le=20),
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """
    Return the FULL nested tree in one call — VFS sidebar friendly.
    Walks parent_id relationships recursively (bounded by max_depth).
    """
    org_id = user["org_id"]
    import uuid as _uuid
    try:
        _uuid.UUID(str(org_id))
    except (ValueError, AttributeError):
        org_id = "00000000-0000-0000-0000-000000000001"

    async def build_node(asset) -> Dict:
        return {
            "id": str(asset.id),
            "name": asset.name,
            "asset_type": asset.asset_type,
            "status": asset.status,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "children": [],
        }

    async def walk(parent_id: Optional[str], depth: int) -> List[Dict]:
        if depth > max_depth:
            return []
        items, _ = await asset_repo.list(
            db, org_id=org_id, project_id=project_id,
            parent_id=parent_id,
            parent_id_is_null=(parent_id is None),
            limit=500,
        )
        nodes = []
        for item in items:
            node = await build_node(item)
            if item.asset_type == "folder":
                node["children"] = await walk(str(item.id), depth + 1)
            nodes.append(node)
        return nodes

    if root_id:
        root = await asset_repo.get_by_id(db, root_id, org_id)
        if not root:
            raise HTTPException(status_code=404, detail="root_id not found")
        tree_root = await build_node(root)
        tree_root["children"] = await walk(root_id, 1)
        return tree_root
    else:
        children = await walk(None, 1)
        return {
            "id": None,
            "name": "root",
            "asset_type": "root",
            "children": children,
        }


@router.get("/{asset_id}")
async def get_asset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Get asset by ID."""
    asset = await asset_repo.get_by_id(db, asset_id, user["org_id"])
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _asset_to_dict(asset)


async def _delete_blob_from_storage(blob_ref: Optional[str]):
    """Delete the underlying object from MinIO — called after soft delete."""
    if not blob_ref:
        return
    try:
        import aioboto3
        from app.core.config import settings
        session = aioboto3.Session()
        async with session.client(
            "s3", endpoint_url=settings.STORAGE_ENDPOINT,
            aws_access_key_id=settings.STORAGE_ACCESS_KEY,
            aws_secret_access_key=settings.STORAGE_SECRET_KEY,
        ) as s3:
            await s3.delete_object(Bucket=settings.STORAGE_BUCKET, Key=blob_ref)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to delete blob {blob_ref}: {e}")


@router.delete("/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(require_permission("assets", "delete")),
):
    """Soft delete asset + remove from search index + clean up storage blob."""
    asset = await asset_repo.get_by_id(db, asset_id, user["org_id"])
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    deleted = await asset_repo.soft_delete(db, asset_id, user["org_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Asset not found")

    background_tasks.add_task(search_service.delete_from_index, asset_id, user["org_id"])
    background_tasks.add_task(_delete_blob_from_storage, asset.blob_ref)

    await publish_event(EventType.ASSET_DELETED, {
        "asset_id": asset_id,
        "org_id": user["org_id"],
    })
