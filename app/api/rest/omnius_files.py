"""
Path-based read/list bridge for dunemachines_backend's Omnius agent tools —
Option 1 of the "perfect sync" fix (agent reads must reflect the same file
tree the user sees, not a locally-cached copy that can silently diverge).

This repo has no path-based asset API anywhere else — every other endpoint
addresses an asset by UUID, walking the parent_id tree only when a caller
already has an id (see acl.py's _resolve_path for the one prior example of
walking a path). These two endpoints are new: dunemachines_backend's
read_file/list_files tools need to resolve a workspace-relative path
("bakery-app/src/App.jsx") to actual content/listing in one call each.

PER-USER ISOLATION (a real, separate bug found and fixed here): the
existing sync path in dunemachines_backend (fileserver_sync.py's
get_or_create_omnius_root) reused a single folder literally named
"Omnius Projects", found by name alone in the ORG-wide tree — every user
in the same org collapsed onto the SAME folder, so one user's agent-
written files would land in the same parent as another org member's.
Fixed on the dunemachines_backend side by naming that folder uniquely per
user (see fileserver_sync.py's updated get_or_create_omnius_root); this
module resolves against whatever root folder name the caller is already
using, so it doesn't need to know that naming scheme itself — it just
finds-or-creates a folder named `_user_root_name(user_id)` under the org
root and treats that as the read/list root, exactly mirroring the write
side's own resolution so both sides always agree on where "the workspace"
lives in this org's asset tree.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.core.security import get_current_user, decode_token, security
from app.repositories.asset_repo import asset_repo
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/omnius-files", tags=["omnius-files"])

_MAX_PATH_SEGMENTS = 32
_MAX_READ_BYTES = 10 * 1024 * 1024  # matches ReadFileTool's own local-disk cap


def _user_root_name(raw_sub: str) -> str:
    """Must match dunemachines_backend's fileserver_sync.py
    get_or_create_user_root naming exactly — both sides resolve the same
    folder by name, there is no shared id to key off of instead.

    Takes the token's RAW `sub` claim, not get_current_user's resolved
    identity: dunemachines_backend always names this folder using the
    plain numeric user_id string it already has (e.g. "50618"), with no
    derivation. get_current_user's resolve_identity, by contrast, turns
    a non-UUID sub into a DERIVED uuid5 string for its own internal
    identity — a real bug caught during live verification, where the two
    sides silently disagreed and every read 404'd despite a successful
    write. Using the raw sub directly here keeps both sides in exact
    agreement without duplicating resolve_identity's hashing elsewhere.
    """
    return f"Omnius Workspace ({raw_sub})"


async def _agent_raw_sub(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """The raw `sub` claim from the bearer token — see _user_root_name
    for why this, not get_current_user's resolved user_id, is what must
    be used to name/find a user's workspace root."""
    payload = decode_token(credentials.credentials)
    return str(payload.get("sub") or payload.get("uid") or "")


class ListEntry(BaseModel):
    name: str
    is_directory: bool
    size: int = 0
    mime_type: Optional[str] = None


async def _get_user_root(db: AsyncSession, org_id: str, user_id: str) -> Optional[object]:
    """Finds (never creates — a read/list request has nothing to write)
    this user's workspace root folder. None means the user has no synced
    content yet at all, which callers treat as "not found", not an error."""
    items, _ = await asset_repo.list(
        db, org_id=org_id, parent_id=None, parent_id_is_null=True, limit=500,
    )
    name = _user_root_name(user_id)
    return next((a for a in items if a.name == name and a.asset_type == "folder"), None)


async def _resolve(db: AsyncSession, org_id: str, root, path: str) -> Optional[object]:
    """Walks from root (the user's workspace root asset) down `path`.
    Empty/"."/"/" path resolves to root itself."""
    segments = [s for s in (path or "").strip("/").split("/") if s and s != "."][:_MAX_PATH_SEGMENTS]
    if not segments:
        return root

    parent_id = str(root.id)
    asset = None
    for segment in segments:
        items, _ = await asset_repo.list(
            db, org_id=org_id, parent_id=parent_id, parent_id_is_null=False, limit=500,
        )
        match = next((a for a in items if a.name == segment), None)
        if match is None:
            return None
        asset = match
        parent_id = str(match.id)
    return asset


@router.get("/read")
async def read_file(
    path: str = Query(..., description="Workspace-relative path, e.g. bakery-app/src/App.jsx"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    raw_sub: str = Depends(_agent_raw_sub),
):
    """Returns raw file bytes with the asset's own mime_type — the caller
    (dunemachines_backend's fileserver read client) decodes text itself,
    same as it already does for a local-disk read, so this endpoint
    doesn't need to guess text vs binary."""
    root = await _get_user_root(db, user["org_id"], raw_sub)
    if root is None:
        raise HTTPException(status_code=404, detail="No workspace content synced for this user yet")

    asset = await _resolve(db, user["org_id"], root, path)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    if asset.asset_type == "folder":
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")
    if not asset.blob_ref:
        raise HTTPException(status_code=404, detail=f"File has no content yet: {path}")
    if (asset.size_bytes or 0) > _MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large: {asset.size_bytes} bytes (max {_MAX_READ_BYTES})")

    from app.core.s3_client import get_s3_client
    s3 = get_s3_client()
    try:
        obj = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=asset.blob_ref)
        content = await obj["Body"].read()
    except Exception as e:
        logger.warning(f"[OmniusFiles] blob fetch failed for {path} ({asset.blob_ref}): {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch file content from storage")

    return Response(content=content, media_type=asset.mime_type or "application/octet-stream")


@router.get("/list", response_model=List[ListEntry])
async def list_files(
    path: str = Query(default="", description="Workspace-relative directory path, empty for root"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    raw_sub: str = Depends(_agent_raw_sub),
):
    root = await _get_user_root(db, user["org_id"], raw_sub)
    if root is None:
        raise HTTPException(status_code=404, detail="No workspace content synced for this user yet")

    target = await _resolve(db, user["org_id"], root, path)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    if target.asset_type != "folder":
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    items, _ = await asset_repo.list(
        db, org_id=user["org_id"], parent_id=str(target.id), parent_id_is_null=False, limit=1000,
    )
    return [
        ListEntry(
            name=i.name,
            is_directory=(i.asset_type == "folder"),
            size=i.size_bytes or 0,
            mime_type=i.mime_type,
        )
        for i in items
    ]
