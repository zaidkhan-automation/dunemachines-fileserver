from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user
from app.services.permissions.rbac import require_permission
from app.repositories.project_repo import project_repo

router = APIRouter(prefix="/projects", tags=["projects"])

DEFAULT_ORG = "00000000-0000-0000-0000-000000000001"
DEFAULT_USER = "00000000-0000-0000-0000-000000000000"


def _safe_uuid(val: Optional[str], fallback: str) -> str:
    if not val:
        return fallback
    import uuid as _uuid
    try:
        _uuid.UUID(str(val))
        return val
    except (ValueError, AttributeError):
        return fallback


def _project_to_dict(p) -> Dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "description": p.description,
        "is_public": p.is_public,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


class CreateProjectRequest(BaseModel):
    name: str
    description: Optional[str] = None
    is_public: bool = False


@router.post("/")
async def create_project(
    req: CreateProjectRequest,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(require_permission("projects", "write")),
):
    """Create a new project."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)
    created_by = _safe_uuid(user.get("user_id"), DEFAULT_USER)

    project = await project_repo.create(db, {
        "organization_id": org_id,
        "created_by": created_by,
        "name": req.name,
        "description": req.description,
        "is_public": req.is_public,
    })
    return _project_to_dict(project)


@router.get("/")
async def list_projects(
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """List all projects."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)
    projects, total = await project_repo.list(db, org_id=org_id, limit=limit, offset=offset)
    return {
        "projects": [_project_to_dict(p) for p in projects],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(get_current_user),
):
    """Get project by ID."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)
    project = await project_repo.get_by_id(db, project_id, org_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_to_dict(project)


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: Dict = Depends(require_permission("projects", "write")),
):
    """Delete project."""
    org_id = _safe_uuid(user.get("org_id"), DEFAULT_ORG)
    deleted = await project_repo.delete(db, project_id, org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"id": project_id, "deleted": True}
