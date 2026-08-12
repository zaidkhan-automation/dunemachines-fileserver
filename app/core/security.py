"""
JWT authentication — supports both Duniverse tokens and native file server tokens.
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from jose import JWTError, jwt
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.config import settings

security = HTTPBearer()

# Tokens bridged in from Duniverse (native login, no fileserver RBAC concept)
# never carry a roles claim. Default them to editor rather than zero
# permissions, matching the implicit full trust already granted to any
# authenticated user by the unguarded folder endpoints.
DEFAULT_BRIDGED_ROLES = ["editor"]

# Duniverse-bridged tokens carry a username in `sub` and no org_id claim at
# all. Every such token used to fall back to the SAME hardcoded org_id/user_id
# constants below, which meant every Duniverse user shared one org bucket —
# a cross-tenant data leak (any bridged user could read/write/delete any
# other bridged user's files). uuid5 is deterministic: the same username
# always derives the same org_id/user_id, so a user's files are still there
# on their next login, but no two different usernames ever collide.
_BRIDGED_ORG_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "dunemachines-fileserver/duniverse-bridged-org")
_BRIDGED_USER_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "dunemachines-fileserver/duniverse-bridged-user")


async def resolve_identity(payload: Dict[str, Any]) -> Dict[str, str]:
    """Derive a stable (user_id, org_id) pair from a decoded JWT payload.

    Already-UUID subs (native file-server tokens) pass through unchanged.
    Non-UUID subs (Duniverse username bridge) get a per-username UUID
    instead of collapsing onto a shared constant.

    org_id resolution order: a live lookup against omnius_db's
    user_active_org (by the token's numeric "uid" claim, or its numeric
    "sub" for dunemachines_backend-minted agent tokens) — so a user
    switching org takes effect on their very next request, no new token
    needed — then the token's own org_id claim as a fail-open fallback
    for when that lookup can't reach omnius_db, then per-sub derivation.
    """
    sub = str(payload.get("sub") or payload.get("user_id") or "")

    try:
        uuid.UUID(sub)
        user_id = sub
    except ValueError:
        user_id = str(uuid.uuid5(_BRIDGED_USER_NAMESPACE, sub)) if sub else "00000000-0000-0000-0000-000000000000"

    numeric_uid = payload.get("uid")
    if numeric_uid is None and sub.isdigit():
        numeric_uid = sub
    org_id = None
    if numeric_uid is not None:
        from app.core.omnius_client import get_active_org_id
        org_id = await get_active_org_id(int(numeric_uid))

    org_id = org_id or payload.get("org_id") or (
        str(uuid.uuid5(_BRIDGED_ORG_NAMESPACE, sub)) if sub else "00000000-0000-0000-0000-000000000001"
    )

    return {"user_id": user_id, "org_id": org_id}


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, secret: str = None) -> Dict[str, Any]:
    secret = secret or settings.JWT_SECRET
    try:
        payload = jwt.decode(token, secret, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def decode_duniverse_token(token: str) -> Dict[str, Any]:
    """Decode Duniverse JWT — allows Duniverse users to use file server directly."""
    return decode_token(token, secret=settings.DUNIVERSE_JWT_SECRET)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> Dict[str, Any]:
    """
    Dependency — extracts user from JWT.
    Tries native token first, then Duniverse token.
    Returns: {user_id, org_id, email, roles}
    """
    token = credentials.credentials

    # Try native token
    try:
        payload = decode_token(token)
        identity = await resolve_identity(payload)
        return {
            "user_id": identity["user_id"],
            "org_id": identity["org_id"],
            "email": payload.get("email", ""),
            "roles": payload.get("roles") or DEFAULT_BRIDGED_ROLES,
            "source": "native",
        }
    except HTTPException:
        pass

    # Try Duniverse token
    try:
        payload = decode_duniverse_token(token)
        identity = await resolve_identity(payload)
        return {
            "user_id": identity["user_id"],
            "org_id": identity["org_id"],
            "email": payload.get("email", ""),
            "roles": payload.get("roles") or DEFAULT_BRIDGED_ROLES,
            "source": "duniverse",
        }
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Optional auth — doesn't raise if no token
async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(HTTPBearer(auto_error=False)),
) -> Optional[Dict[str, Any]]:
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None
