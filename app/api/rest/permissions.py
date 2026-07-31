"""
Permissions & API Keys endpoints.
Admins can assign roles, create scoped tokens for agents/collaborators.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user, create_access_token
from app.services.permissions.rbac import BUILT_IN_ROLES, PermissionChecker

router = APIRouter(prefix="/permissions", tags=["permissions"])


# ── Schemas ───────────────────────────────────────────────────────

class AssignRoleRequest(BaseModel):
    user_id: str
    role: str = Field(..., description="owner|admin|editor|viewer|agent:read|agent:write|agent:full")
    resource_type: str = Field(default="org", description="org|project|asset")
    resource_id: Optional[str] = None


class CreateAgentTokenRequest(BaseModel):
    name: str = Field(..., description="Agent name e.g. 'Omnius AI Agent'")
    role: str = Field(default="agent:write", description="agent:read|agent:write|agent:full")
    scopes: Optional[List[str]] = Field(default=None, description="Additional custom scopes")
    project_id: Optional[str] = Field(default=None, description="Limit to specific project")
    expires_in_days: int = Field(default=30, ge=1, le=365)


class CreateCollaboratorTokenRequest(BaseModel):
    name: str = Field(..., description="Collaborator name")
    role: str = Field(default="viewer", description="editor|viewer")
    project_id: Optional[str] = None
    expires_in_days: int = Field(default=7, ge=1, le=90)


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/roles")
async def list_roles(user: Dict = Depends(get_current_user)):
    """List all available roles and their permissions."""
    return {
        "built_in_roles": [
            {
                "role": role_id,
                "name": role_data["name"],
                "description": role_data["description"],
                "permission_count": len(role_data["permissions"]),
                "permissions": sorted(role_data["permissions"]),
                "is_system": role_data.get("is_system", False),
            }
            for role_id, role_data in BUILT_IN_ROLES.items()
        ],
        "total": len(BUILT_IN_ROLES),
    }


@router.get("/roles/{role_id}")
async def get_role(role_id: str, user: Dict = Depends(get_current_user)):
    """Get detailed permissions for a specific role."""
    role = BUILT_IN_ROLES.get(role_id)
    if not role:
        raise HTTPException(status_code=404, detail=f"Role '{role_id}' not found")
    return {
        "role": role_id,
        "name": role["name"],
        "description": role["description"],
        "permissions": sorted(role["permissions"]),
        "permission_count": len(role["permissions"]),
    }


@router.get("/my-permissions")
async def get_my_permissions(user: Dict = Depends(get_current_user)):
    """Get current user/agent's effective permissions."""
    checker = PermissionChecker.from_token(user)
    roles = user.get("roles", [])

    permissions = checker.get_permissions()

    return {
        "user_id": user.get("user_id"),
        "roles": roles,
        "effective_permissions": sorted(permissions),
        "permission_count": len(permissions),
        "is_admin": "admin" in roles or "owner" in roles,
        "is_agent": any(r.startswith("agent:") for r in roles),
    }


@router.post("/agent-token")
async def create_agent_token(
    req: CreateAgentTokenRequest,
    user: Dict = Depends(get_current_user),
):
    """
    Create a scoped JWT token for an AI agent.
    Only admins/owners can create agent tokens.
    """
    roles = user.get("roles", [])
    if "owner" not in roles and "admin" not in roles:
        raise HTTPException(status_code=403, detail="Only admins can create agent tokens")

    # Get role permissions
    role_data = BUILT_IN_ROLES.get(req.role)
    if not role_data:
        raise HTTPException(status_code=400, detail=f"Invalid role: {req.role}")

    # Prevent privilege escalation — admin cannot create owner/admin tokens
    requester_roles = user.get("roles", [])
    if req.role in ["owner", "admin"] and "owner" not in requester_roles:
        raise HTTPException(status_code=403, detail="Cannot create tokens with higher privilege than your own role")

    # Build scopes
    scopes = list(role_data["permissions"])
    if req.scopes:
        scopes.extend(req.scopes)

    # Create token
    from datetime import timedelta
    token_data = {
        "sub": f"agent:{req.name.lower().replace(' ', '_')}",
        "name": req.name,
        "type": "agent",
        "org_id": user["org_id"],
        "roles": [req.role],
        "scopes": list(set(scopes)),
        "created_by": user["user_id"],
    }
    if req.project_id:
        token_data["project_id"] = req.project_id

    token = create_access_token(
        token_data,
        expires_delta=timedelta(days=req.expires_in_days)
    )

    return {
        "token": token,
        "agent_name": req.name,
        "role": req.role,
        "scopes": sorted(set(scopes)),
        "scope_count": len(set(scopes)),
        "project_id": req.project_id,
        "expires_in_days": req.expires_in_days,
        "warning": "Store this token securely — it will not be shown again",
    }


@router.post("/collaborator-token")
async def create_collaborator_token(
    req: CreateCollaboratorTokenRequest,
    user: Dict = Depends(get_current_user),
):
    """
    Create a scoped JWT token for a human collaborator.
    Only admins/owners can invite collaborators.
    """
    roles = user.get("roles", [])
    if "owner" not in roles and "admin" not in roles:
        raise HTTPException(status_code=403, detail="Only admins can create collaborator tokens")

    # Only allow non-admin roles for collaborators
    allowed_roles = ["editor", "viewer"]
    if req.role not in allowed_roles:
        raise HTTPException(status_code=400, detail=f"Collaborator role must be one of: {allowed_roles}")

    role_data = BUILT_IN_ROLES.get(req.role)
    scopes = list(role_data["permissions"])

    from datetime import timedelta
    token_data = {
        "sub": f"collaborator:{req.name.lower().replace(' ', '_')}",
        "name": req.name,
        "type": "collaborator",
        "org_id": user["org_id"],
        "roles": [req.role],
        "scopes": scopes,
        "created_by": user["user_id"],
    }
    if req.project_id:
        token_data["project_id"] = req.project_id

    token = create_access_token(
        token_data,
        expires_delta=timedelta(days=req.expires_in_days)
    )

    return {
        "token": token,
        "collaborator_name": req.name,
        "role": req.role,
        "permissions": sorted(scopes),
        "project_id": req.project_id,
        "expires_in_days": req.expires_in_days,
        "warning": "Share this token securely with the collaborator",
    }


@router.post("/check")
async def check_permission(
    resource: str,
    action: str,
    user: Dict = Depends(get_current_user),
):
    """Check if current user has a specific permission."""
    checker = PermissionChecker.from_token(user)
    has_perm = checker.has(resource, action)
    roles = user.get("roles", [])

    return {
        "permission": f"{resource}:{action}",
        "granted": has_perm or "owner" in roles or "admin" in roles,
        "roles": roles,
        "reason": "role:owner/admin" if ("owner" in roles or "admin" in roles) else
                  "direct" if has_perm else "denied",
    }
