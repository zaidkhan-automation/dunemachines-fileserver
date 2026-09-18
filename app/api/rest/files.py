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
            "children_truncated": False,
        }

    async def walk(parent_id: Optional[str], depth: int) -> tuple[List[Dict], bool]:
        if depth > max_depth:
            return [], False
        items, total = await asset_repo.list(
            db, org_id=org_id, project_id=project_id,
            parent_id=parent_id,
            parent_id_is_null=(parent_id is None),
            limit=500,
        )
        truncated = total > len(items)
        nodes = []
        for item in items:
            node = await build_node(item)
            if item.asset_type == "folder":
                node["children"], node["children_truncated"] = await walk(str(item.id), depth + 1)
            nodes.append(node)
        return nodes, truncated

    if root_id:
        root = await asset_repo.get_by_id(db, root_id, org_id)
        if not root:
            raise HTTPException(status_code=404, detail="root_id not found")
        tree_root = await build_node(root)
        tree_root["children"], tree_root["children_truncated"] = await walk(root_id, 1)
        return tree_root
    else:
        children, truncated = await walk(None, 1)
        return {
            "id": None,
            "name": "root",
            "asset_type": "root",
            "children": children,
            "children_truncated": truncated,
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


@router.get("/{asset_id}/content")
async def get_asset_content(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Streams blob bytes for any asset in the caller's org, org-scoped
    exactly like get_asset above (asset_repo.get_by_id already filters by
    user["org_id"] — no separate permission check needed for read, same
    as list_assets/get_asset).

    Added for the Org Files Unification cloud->local hydration path
    (dunemachines_backend's fileserver_sync.hydrate_user_from_fileserver):
    that needs to download ANY asset under the canonical org tree
    (GET /assets/tree, org-root-scoped), not just ones reachable through
    omnius_files.py's /read bridge — that endpoint resolves a path
    relative to the caller's own "Omnius Workspace ({uid})" subfolder
    only, so it can't reach a sibling top-level asset or a teammate's
    file elsewhere in the same org tree, which hydration must be able to
    do to represent the tree the frontend actually shows.

    F-11 (remediation audit): streams the object in chunks via
    StreamingResponse instead of materializing the whole blob in memory
    with a single .read() — this endpoint has no size cap, so a large
    asset used to mean a large in-process buffer per concurrent request.
    Same authorization and error semantics as before for the INITIAL
    fetch (404 missing/folder/no-blob-ref, 502 if get_object itself
    fails) — a failure that happens mid-stream, after a 200 has already
    started, can no longer become a clean HTTP error status; that's an
    inherent trade-off of streaming, not something this change can avoid.

    F-07 (remediation audit): unlike download-url (which already forces
    Content-Disposition: attachment via generate_presigned_get's
    filename param), this endpoint used to return the caller-declared
    mime_type with no Content-Disposition and no
    X-Content-Type-Options — a browser opening this URL directly would
    render whatever mime_type was declared at upload time (or, without
    nosniff, might sniff-and-render even a blandly-declared file if its
    actual bytes look like HTML). Both are closed here: nosniff is now
    always sent, and anything whose stored mime_type — or a sniffed
    'dangerous' flag recorded at upload completion, see
    upload_service._sniff_content_type — falls in
    DANGEROUS_RENDER_MIME_TYPES is forced to attachment disposition
    regardless of what was declared. Arbitrary file types are still
    fully supported; only in-browser rendering of active content is
    restricted."""
    asset = await asset_repo.get_by_id(db, asset_id, user["org_id"])
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.asset_type == "folder":
        raise HTTPException(status_code=400, detail="Not a file")
    if not asset.blob_ref:
        raise HTTPException(status_code=404, detail="File has no content yet")

    from fastapi.responses import StreamingResponse
    from app.core.config import settings
    from app.core.s3_client import get_s3_client
    from app.services.uploads.upload_service import DANGEROUS_RENDER_MIME_TYPES
    import logging

    s3 = get_s3_client()
    try:
        obj = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=asset.blob_ref)
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"[Assets] blob fetch failed for {asset_id} ({asset.blob_ref}): {type(e).__name__}: {e}"
        )
        raise HTTPException(status_code=502, detail="Failed to fetch file content from storage")

    body = obj["Body"]

    async def _stream_body():
        try:
            async for chunk in body.iter_chunks():
                yield chunk
        finally:
            # Best-effort — releases the underlying HTTP connection back
            # to the shared s3 client's pool on early disconnect (body
            # fully consumed to EOF already releases it on its own; this
            # only matters for the abandoned-mid-stream case). Never lets
            # a cleanup failure surface past the generator.
            try:
                body.close()
            except Exception:
                pass

    normalized_mime = (asset.mime_type or "application/octet-stream").strip().lower().split(";", 1)[0].strip()
    sniff_flagged_dangerous = bool((asset.extra_data or {}).get("content_sniff", {}).get("dangerous"))
    headers = {"X-Content-Type-Options": "nosniff"}
    if normalized_mime in DANGEROUS_RENDER_MIME_TYPES or sniff_flagged_dangerous:
        headers["Content-Disposition"] = f'attachment; filename="{asset.name}"'

    return StreamingResponse(_stream_body(), media_type=asset.mime_type or "application/octet-stream", headers=headers)


async def _delete_blob_from_storage(blob_ref: Optional[str]):
    """Delete the underlying object from MinIO — called after soft delete."""
    if not blob_ref:
        return
    try:
        from app.core.config import settings
        from app.core.s3_client import get_s3_client
        s3 = get_s3_client()
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

    # Revoke any presentation links for this file so GET /p/{token} 404s
    # via resolve_link's existing revoked_at check instead of continuing
    # to serve a deleted file's content — snapshot-mode links in
    # particular never re-check the source asset on resolve, so without
    # this a deleted file's frozen snapshot stayed reachable forever.
    from app.repositories.presentation_link_repo import presentation_link_repo
    await presentation_link_repo.revoke_by_file_id(db, asset_id)

    background_tasks.add_task(search_service.delete_from_index, asset_id, user["org_id"])
    background_tasks.add_task(_delete_blob_from_storage, asset.blob_ref)

    await publish_event(EventType.ASSET_DELETED, {
        "asset_id": asset_id,
        "org_id": user["org_id"],
    })
