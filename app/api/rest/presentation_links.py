"""
Presentation link endpoints — public shareable URLs for markdown decks.

File lives under app/api/rest/ (this codebase's actual convention for
REST routers — see files.py/uploads.py/folders.py), not app/routers/.

Two routers are defined here because access rules split cleanly:
  - `router` (prefix /files/{file_id}/presentation-links and
    /presentation-links/{id}): every route requires auth.
  - `public_router` (prefix /p): GET resolve + POST unlock, NO auth,
    for the actual public viewer.
"""
import uuid
from datetime import datetime
from typing import Dict

from fastapi import APIRouter, HTTPException, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, verify_password, create_unlock_token
from app.core.rate_limit import limiter
from app.services.permissions.rbac import require_permission
from app.repositories.asset_repo import asset_repo
from app.repositories.presentation_link_repo import presentation_link_repo
from app.services import presentation_service
from app.services.presentation_service import (
    ResolveResult, NotMarkdownError, RateLimitedError, PresentationLinkError,
)
from app.schemas.presentation_links import (
    CreateLinkRequest, PresentationLinkResponse, PresentationLinkListItem,
    ResolvedPresentationResponse, UnlockRequest, UnlockResponse, RevokeResponse,
)
from app.core.config import settings

router = APIRouter(tags=["presentation-links"])
public_router = APIRouter(prefix="/p", tags=["presentation-links-public"])


def _link_url(token: str) -> str:
    return f"{settings.PUBLIC_APP_URL.rstrip('/')}/p/{token}"


def _link_to_response(link) -> PresentationLinkResponse:
    return PresentationLinkResponse(
        id=str(link.id),
        token=link.token,
        url=_link_url(link.token),
        fileId=str(link.file_id),
        mode=link.mode,
        revisionId=str(link.revision_id) if link.revision_id else None,
        title=link.title,
        expiresAt=link.expires_at,
        createdAt=link.created_at,
        accessCount=link.access_count,
        options=link.options or {},
    )


async def _org_uuid_or_default(org_id: str) -> str:
    try:
        uuid.UUID(str(org_id))
        return org_id
    except (ValueError, AttributeError):
        return "00000000-0000-0000-0000-000000000001"


def _user_uuid(user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, str(user_id))


@router.post(
    "/files/{file_id}/presentation-links",
    response_model=PresentationLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_presentation_link(
    file_id: str,
    req: CreateLinkRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(require_permission("assets", "read")),
):
    """Create a shareable presentation link for a markdown file. Requires
    view+ (assets:read) permission on the file — the same bar as opening
    it at all."""
    org_id = await _org_uuid_or_default(user["org_id"])
    try:
        link = await presentation_service.create_link(
            db,
            file_id=file_id,
            org_id=org_id,
            user_id=user["user_id"],
            label=req.label,
            expires_at=req.expiresAt,
            password=req.password,
            mode=req.mode,
            options=req.options.model_dump(),
        )
    except RateLimitedError as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))
    except NotMarkdownError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except PresentationLinkError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    return _link_to_response(link)


@router.get("/files/{file_id}/presentation-links", response_model=list[PresentationLinkListItem])
async def list_presentation_links(
    file_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """List all links (active + revoked) for a file. Restricted to the
    file's own creator or an org admin/owner — this codebase has no
    finer-grained per-file ACL than that (asset_repo scopes strictly by
    organization_id, not by individual ownership)."""
    org_id = await _org_uuid_or_default(user["org_id"])
    asset = await asset_repo.get_by_id(db, file_id, org_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    is_creator = str(asset.created_by) == str(_user_uuid(user["user_id"]))
    is_org_admin = any(r in ("owner", "admin") for r in (user.get("roles") or []))
    if not (is_creator or is_org_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    links = await presentation_link_repo.list_for_file(db, file_id)
    return [
        PresentationLinkListItem(
            id=str(l.id), token=l.token, url=_link_url(l.token), mode=l.mode,
            expiresAt=l.expires_at, accessCount=l.access_count,
            revokedAt=l.revoked_at, createdAt=l.created_at,
        )
        for l in links
    ]


@router.delete("/presentation-links/{link_id}", response_model=RevokeResponse)
async def revoke_presentation_link(
    link_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Revoke (soft-delete) a link. Restricted to its creator or an org
    admin/owner."""
    link = await presentation_link_repo.get_by_id(db, link_id)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")

    is_creator = str(link.created_by) == str(_user_uuid(user["user_id"]))
    is_org_admin = any(r in ("owner", "admin") for r in (user.get("roles") or []))
    if not (is_creator or is_org_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    revoked = await presentation_link_repo.revoke(db, link_id)
    return RevokeResponse(success=True, revokedAt=revoked.revoked_at)


# ── Public endpoints (no auth) ──────────────────────────────────────────

@public_router.get("/{token}", response_model=ResolvedPresentationResponse)
@limiter.limit("100/hour")
async def resolve_presentation_link(
    request: Request,
    token: str,
    unlock: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Public resolve — no auth. Same 404 for invalid/expired/revoked (no
    metadata leak); 403 only for a set-but-not-yet-unlocked password."""
    result = await presentation_service.resolve_link(db, token, unlock_token=unlock)
    if result.status == ResolveResult.NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if result.status == ResolveResult.PASSWORD_REQUIRED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password required")
    return ResolvedPresentationResponse(**result.data)


def _unlock_rate_key(request: Request) -> str:
    from slowapi.util import get_remote_address
    token = request.path_params.get("token", "")
    return f"{get_remote_address(request)}:{token}"


@public_router.post("/{token}/unlock", response_model=UnlockResponse)
@limiter.limit("10/hour", key_func=_unlock_rate_key)
async def unlock_presentation_link(
    request: Request,
    token: str,
    req: UnlockRequest,
    db: AsyncSession = Depends(get_db),
):
    """Public unlock — no auth, rate-limited per IP+token. Verifies the
    password and, on success, mints a 5-minute JWT scoped to this one
    link token (see create_unlock_token) for use as ?unlock=... on the
    resolve endpoint."""
    link = await presentation_link_repo.get_by_token(db, token)
    if link is None or link.revoked_at is not None or (
        link.expires_at is not None and link.expires_at < datetime.utcnow()
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if link.password_hash is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This link has no password")

    if not verify_password(req.password, link.password_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Incorrect password")

    return UnlockResponse(success=True, token=create_unlock_token(token))
