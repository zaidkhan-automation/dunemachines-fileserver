"""
Production-grade RBAC Permission System
Fully extendable — admins control every aspect.

Permission structure:
  resource_type:action
  e.g. assets:read, assets:write, projects:admin, connectors:sync

Roles are collections of permissions.
Custom roles can be created per org.
"""
from enum import Enum
from typing import List, Set, Dict, Optional


# ── Permission Actions ────────────────────────────────────────────

class Action(str, Enum):
    # CRUD
    READ   = "read"
    WRITE  = "write"
    DELETE = "delete"
    LIST   = "list"

    # Advanced
    ADMIN    = "admin"    # Full control
    SHARE    = "share"    # Share with others
    DOWNLOAD = "download" # Download files
    UPLOAD   = "upload"   # Upload files

    # Connector specific
    SYNC     = "sync"     # Trigger sync
    CONNECT  = "connect"  # Connect new connector
    WEBHOOK  = "webhook"  # Manage webhooks

    # Agent specific
    EXECUTE  = "execute"  # Run tools/actions
    SEARCH   = "search"   # Semantic search
    EMBED    = "embed"    # Generate embeddings
    SUMMARIZE = "summarize" # Generate summaries

    # GitHub specific
    PR_CREATE  = "pr:create"
    PR_MERGE   = "pr:merge"
    PR_COMMENT = "pr:comment"
    COMMIT     = "commit"
    BRANCH     = "branch:create"

    # Organization
    ORG_MANAGE  = "org:manage"
    ORG_BILLING = "org:billing"
    ORG_DELETE  = "org:delete"

    # Members
    MEMBERS_INVITE  = "members:invite"
    MEMBERS_REMOVE  = "members:remove"
    MEMBERS_MANAGE  = "members:manage"

    # Audit
    AUDIT_VIEW = "audit:view"
    AUDIT_EXPORT = "audit:export"


# ── Resource Types ────────────────────────────────────────────────

class Resource(str, Enum):
    ASSET      = "assets"
    PROJECT    = "projects"
    FOLDER     = "folders"
    CONNECTOR  = "connectors"
    WEBHOOK    = "webhooks"
    MEMBER     = "members"
    ORG        = "org"
    AGENT      = "agents"
    AUDIT      = "audit"
    SEARCH     = "search"
    STORAGE    = "storage"
    API_KEY    = "api_keys"


# ── Permission ────────────────────────────────────────────────────

class Permission:
    """
    A single permission: resource:action
    e.g. assets:read, projects:admin, connectors:sync
    """
    def __init__(self, resource: Resource, action: Action):
        self.resource = resource
        self.action = action
        self.key = f"{resource.value}:{action.value}"

    def __str__(self):
        return self.key

    def __eq__(self, other):
        return self.key == str(other)

    def __hash__(self):
        return hash(self.key)

    @classmethod
    def from_string(cls, perm_str: str) -> "Permission":
        parts = perm_str.split(":")
        if len(parts) < 2:
            raise ValueError(f"Invalid permission: {perm_str}")
        resource = parts[0]
        action = ":".join(parts[1:])
        return cls(Resource(resource), Action(action))


# ── Built-in Permission Sets ──────────────────────────────────────

VIEWER_PERMISSIONS: Set[str] = {
    "assets:read",
    "assets:list",
    "assets:download",
    "assets:search",
    "projects:read",
    "projects:list",
    "folders:read",
    "folders:list",
    "search:read",
}

EDITOR_PERMISSIONS: Set[str] = VIEWER_PERMISSIONS | {
    "assets:write",
    "assets:upload",
    "assets:delete",
    "folders:write",
    "folders:delete",
    "projects:write",
    "connectors:read",
    "connectors:sync",
    "search:read",
}

AGENT_READ_PERMISSIONS: Set[str] = {
    "assets:read",
    "assets:list",
    "assets:search",
    "assets:download",
    "projects:read",
    "projects:list",
    "search:read",
    "agents:execute",
}

AGENT_WRITE_PERMISSIONS: Set[str] = AGENT_READ_PERMISSIONS | {
    "assets:write",
    "assets:upload",
    "assets:delete",
    "assets:summarize",
    "assets:embed",
    "folders:write",
    "connectors:sync",
}

AGENT_FULL_PERMISSIONS: Set[str] = AGENT_WRITE_PERMISSIONS | {
    "connectors:connect",
    "webhooks:read",
    "connectors:pr:create",
    "connectors:pr:merge",
    "connectors:pr:comment",
    "connectors:commit",
    "connectors:branch:create",
}

ADMIN_PERMISSIONS: Set[str] = EDITOR_PERMISSIONS | {
    "connectors:connect",
    "connectors:webhook",
    "members:invite",
    "members:remove",
    "members:manage",
    "api_keys:read",
    "api_keys:write",
    "api_keys:delete",
    "audit:view",
    "storage:read",
    "org:manage",
    "agents:read",
    "agents:write",
}

OWNER_PERMISSIONS: Set[str] = ADMIN_PERMISSIONS | {
    "org:billing",
    "org:delete",
    "audit:export",
    "members:manage",
}


# ── Built-in Roles ────────────────────────────────────────────────

BUILT_IN_ROLES: Dict[str, Dict] = {
    "owner": {
        "name": "Owner",
        "description": "Full control over organization",
        "permissions": list(OWNER_PERMISSIONS),
        "is_system": True,
    },
    "admin": {
        "name": "Admin",
        "description": "Manage team and connectors, cannot delete org",
        "permissions": list(ADMIN_PERMISSIONS),
        "is_system": True,
    },
    "editor": {
        "name": "Editor",
        "description": "Create, edit, upload assets and sync connectors",
        "permissions": list(EDITOR_PERMISSIONS),
        "is_system": True,
    },
    "viewer": {
        "name": "Viewer",
        "description": "Read-only access to assets and projects",
        "permissions": list(VIEWER_PERMISSIONS),
        "is_system": True,
    },
    "agent:read": {
        "name": "Agent (Read Only)",
        "description": "AI agent with read-only access — search, list, download",
        "permissions": list(AGENT_READ_PERMISSIONS),
        "is_system": True,
    },
    "agent:write": {
        "name": "Agent (Read/Write)",
        "description": "AI agent with read/write access — upload, summarize, embed",
        "permissions": list(AGENT_WRITE_PERMISSIONS),
        "is_system": True,
    },
    "agent:full": {
        "name": "Agent (Full)",
        "description": "AI agent with full access — all ops including GitHub actions",
        "permissions": list(AGENT_FULL_PERMISSIONS),
        "is_system": True,
    },
}


# ── Permission Checker ────────────────────────────────────────────

class PermissionChecker:
    """
    Check if a user/agent has a specific permission.
    Supports: role-based, direct permissions, wildcard.
    """

    def __init__(self, user_permissions: Set[str]):
        self.permissions = user_permissions

    def has(self, resource: str, action: str) -> bool:
        """Check single permission."""
        perm = f"{resource}:{action}"

        # Direct match
        if perm in self.permissions:
            return True

        # Wildcard resource admin
        if f"{resource}:admin" in self.permissions:
            return True

        # Global admin
        if "org:admin" in self.permissions:
            return True

        return False

    def has_any(self, *perms: tuple) -> bool:
        """Check if has any of the given permissions."""
        return any(self.has(r, a) for r, a in perms)

    def has_all(self, *perms: tuple) -> bool:
        """Check if has all of the given permissions."""
        return all(self.has(r, a) for r, a in perms)

    def get_permissions(self) -> List[str]:
        return list(self.permissions)

    @classmethod
    def from_role(cls, role: str) -> "PermissionChecker":
        """Create checker from built-in role name."""
        role_data = BUILT_IN_ROLES.get(role, {})
        permissions = set(role_data.get("permissions", []))
        return cls(permissions)

    @classmethod
    def from_token(cls, token_payload: dict) -> "PermissionChecker":
        """Create checker from JWT payload."""
        roles = token_payload.get("roles", [])
        scopes = token_payload.get("scopes", [])

        permissions = set(scopes)  # Direct scopes from token

        # Add role permissions
        for role in roles:
            role_data = BUILT_IN_ROLES.get(role, {})
            permissions.update(role_data.get("permissions", []))

        return cls(permissions)


# ── Transport-agnostic checks ──────────────────────────────────────
# Core enforcement logic, usable outside FastAPI's Depends() — e.g. from
# GraphQL resolvers, which have no dependency-injection mechanism of their
# own and would otherwise end up with a second, drifting copy of this logic.

def check_permission(user: dict, resource: str, action: str) -> bool:
    """Does `user` (a resolved identity dict with roles/scopes) have resource:action?"""
    roles = user.get("roles", [])
    if "owner" in roles or "admin" in roles:
        return True
    return PermissionChecker.from_token(user).has(resource, action)


def check_any_permission(user: dict, *perms) -> bool:
    """Does `user` have any of the given (resource, action) permissions?"""
    roles = user.get("roles", [])
    if "owner" in roles or "admin" in roles:
        return True
    return PermissionChecker.from_token(user).has_any(*perms)


# ── FastAPI Dependencies ──────────────────────────────────────────

def require_permission(resource: str, action: str):
    """FastAPI dependency — require specific permission."""
    from fastapi import Depends, HTTPException, status
    from app.core.security import get_current_user

    async def check(user: dict = Depends(get_current_user)):
        if not check_permission(user, resource, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {resource}:{action}"
            )
        return user

    return check


def require_any_permission(*perms):
    """FastAPI dependency — require any of the given permissions."""
    from fastapi import Depends, HTTPException, status
    from app.core.security import get_current_user

    async def check(user: dict = Depends(get_current_user)):
        if not check_any_permission(user, *perms):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied. Required one of: {perms}"
            )
        return user

    return check
