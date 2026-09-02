"""
ACL check endpoint — Phase 3 of the Agent Teams Graph rollout on the
dunemachines_backend side (app/core/fileserver_client.py calls this from
app/services/policy_engine.py's evaluate_policy_full, to merge file-level
access into the canMessage decision). See that repo's Phase 3 spec, PART C.

IMPORTANT, documented rather than silently assumed: this fileserver has
no per-file/per-path ACL grant model anywhere in the codebase today.
app/models/permission.py's `permissions` table is defined but genuinely
unused — nothing in this repo reads from it. The real, live permission
system is role-based via JWT roles/scopes checked against the fixed
BUILT_IN_ROLES table (app/services/permissions/rbac.py) — this endpoint's
own sibling, POST /permissions/check, does exactly this same role-based
check, not a table lookup, confirming that's the actual convention here.

So "check permissions table for user_id + each path's asset_id" (the
spec's literal wording) is implemented as: resolve each path to a real
Asset row by walking the parent_id tree from the org root (assets have
no stored full-path column — same tree fileserver_sync.py's
_ensure_folder_path walks from the other side, in dunemachines_backend),
then apply the caller's real org role — resolved live via
resolve_identity/get_current_user, same as every other endpoint in this
repo — through PermissionChecker. can_read/can_write for a given path
therefore only vary by whether an asset exists there at all, not by any
finer per-path grant, because no such grant exists to check.

A path that doesn't resolve to an existing asset is DENIED (can_read =
can_write = False) — this can't distinguish "doesn't exist yet, about to
be created" from "doesn't exist, never will" — flagged as a real gap for
whenever this needs to gate writes to not-yet-created paths, not papered
over here.

The tree walk (one DB query per path segment, no caching or path index)
is deliberately the real cost model, not an optimized fast path — the
Phase 3 latency benchmark run against this endpoint is meant to surface
that real cost, not hide it.
"""
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.repositories.asset_repo import asset_repo
from app.services.permissions.rbac import PermissionChecker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs", tags=["acl"])

_MAX_PATH_SEGMENTS = 16  # defense-in-depth bound on the per-request tree walk


class AclCheckRequest(BaseModel):
    user_id: int
    file_paths: List[str]


async def _resolve_path(db: AsyncSession, org_id: str, file_path: str) -> Optional[object]:
    """Walks the parent_id tree from the org root, matching one path
    segment per level by exact name. Returns the resolved Asset row, or
    None if any segment along the way doesn't exist."""
    segments = [s for s in file_path.strip("/").split("/") if s][:_MAX_PATH_SEGMENTS]
    if not segments:
        return None

    parent_id: Optional[str] = None
    asset = None
    for segment in segments:
        items, _ = await asset_repo.list(
            db, org_id=org_id, parent_id=parent_id,
            parent_id_is_null=(parent_id is None), limit=500,
        )
        match = next((a for a in items if a.name == segment), None)
        if match is None:
            return None
        asset = match
        parent_id = str(match.id)
    return asset


@router.post("/{org_id}/acl/check")
async def check_acl(
    org_id: str,
    body: AclCheckRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
) -> Dict[str, Dict[str, bool]]:
    """Returns {file_path: {can_read, can_write}}. Auth is whichever
    identity the bearer token resolves to (get_current_user) — the
    caller (dunemachines_backend's fileserver_client.check_acl) mints a
    token for the TARGET session's user_id, so this checks whether the
    message's recipient can see the referenced files, not the sender."""
    if user.get("org_id") != org_id:
        # resolve_identity's live DB lookup didn't place this user in the
        # org being queried — deny everything rather than leak whether
        # paths exist in an org this identity has no claim to.
        return {path: {"can_read": False, "can_write": False} for path in body.file_paths}

    checker = PermissionChecker.from_token(user)
    can_read_role = checker.has("assets", "read")
    can_write_role = checker.has("assets", "write")

    result: Dict[str, Dict[str, bool]] = {}
    for path in body.file_paths:
        asset = await _resolve_path(db, org_id, path)
        if asset is None:
            result[path] = {"can_read": False, "can_write": False}
        else:
            result[path] = {"can_read": can_read_role, "can_write": can_write_role}
    return result
