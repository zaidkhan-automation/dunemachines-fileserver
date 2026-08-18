"""
JWT authentication — supports both Duniverse tokens and native file server tokens.
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import bcrypt
from jose import JWTError, jwt
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.config import settings

security = HTTPBearer()

# Tokens bridged in from Duniverse (native login, no fileserver RBAC concept
# of their own) never carry a roles claim. get_current_user's first choice is
# a live lookup of the user's real organization_members role (resolve_identity
# -> get_org_role, Step 3) — this is now only the last-resort fallback for
# when that lookup can't run at all (no numeric uid on the token, or no
# omnius_db organization membership found), matching the implicit full trust
# previously granted unconditionally to any authenticated bridged user.
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



# Step 3: dunemachines_backend's OrgRole vocabulary (viewer/member/editor/
# admin/owner — app/models/organization.py) mapped onto this repo's own
# BUILT_IN_ROLES keys (app/services/permissions/rbac.py). "member" is that
# repo's legacy pre-Step-3 role name; it has no equivalent of its own here
# and collapses onto "editor", the tier it's aliased to on that side too.
_ORG_ROLE_TO_FILESERVER_ROLE = {
    "viewer": "viewer",
    "member": "editor",
    "editor": "editor",
    "admin": "admin",
    "owner": "owner",
}


async def resolve_identity(payload: Dict[str, Any], trust_claims: bool = True) -> Dict[str, Any]:
    """Derive a stable (user_id, org_id, roles) triple from a decoded
    JWT payload.

    Already-UUID subs (native file-server tokens) pass through unchanged.
    Non-UUID subs (Duniverse username bridge) get a per-username UUID
    instead of collapsing onto a shared constant.

    org_id resolution order: a live lookup against omnius_db's
    user_active_org (by the token's numeric "uid" claim, or its numeric
    "sub" for dunemachines_backend-minted agent tokens) — so a user
    switching org takes effect on their very next request, no new token
    needed — then, ONLY when trust_claims is True, the token's own
    org_id claim as a fail-open fallback for when that lookup can't
    reach omnius_db, then per-sub derivation.

    trust_claims must be False for any token whose issuer claim marks it
    as bridged-in from another service (currently "omnius_backend" —
    dunemachines_backend's fileserver_sync.mint_fileserver_token, which
    shares this service's JWT_SECRET for that one bridge). Those claims
    are computed honestly by the minting side, but the raw JWT bytes
    alone can't prove that once a secret is shared across services — so
    for those tokens org_id must come only from the live DB lookup (or
    its safe per-user derivation), never from the claim itself. See
    get_current_user for the roles-claim half of this same rule.

    roles: a live lookup against omnius_db's organization_members for
    the same (numeric_uid, org_id) pair, mapped through
    _ORG_ROLE_TO_FILESERVER_ROLE. None when the lookup can't run (no
    numeric_uid, org_id never resolved to a real membership, or a DB
    problem) — get_current_user falls back to DEFAULT_BRIDGED_ROLES in
    that case, same as before this existed.
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

    claimed_org_id = payload.get("org_id") if trust_claims else None
    org_id = org_id or claimed_org_id or (
        str(uuid.uuid5(_BRIDGED_ORG_NAMESPACE, sub)) if sub else "00000000-0000-0000-0000-000000000001"
    )

    roles = None
    if numeric_uid is not None:
        from app.core.omnius_client import get_org_role
        org_role = await get_org_role(int(numeric_uid), org_id)
        if org_role is not None:
            mapped = _ORG_ROLE_TO_FILESERVER_ROLE.get(org_role)
            if mapped is not None:
                roles = [mapped]

    return {"user_id": user_id, "org_id": org_id, "roles": roles}


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.setdefault("issuer", "fileserver")  # self-issued — safe to trust its own roles/org_id claims
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
        # This service's own JWT_SECRET is also shared with
        # dunemachines_backend for the agent-sync bridge
        # (fileserver_sync.mint_fileserver_token) — a token decoding
        # successfully here is NOT proof it was self-issued. Only an
        # EXPLICIT issuer == "fileserver" claim is trusted; a missing
        # issuer is treated the same as a bridged token, not as
        # implicitly native — an attacker holding the shared secret can
        # forge any field, including issuer itself, so the safest
        # default is "no issuer claim => not proven self-issued". This
        # still doesn't stop someone who also forges issuer="fileserver"
        # (that requires splitting onto a fileserver-exclusive signing
        # secret, deliberately deferred for now), but it does close the
        # trivial no-issuer/omitted-field bypass. Untrusted tokens
        # always fall through to the live DB lookup instead, which is
        # itself fail-open on a DB outage — never trusting the claim as
        # the fallback.
        trust_claims = payload.get("issuer") == "fileserver"
        identity = await resolve_identity(payload, trust_claims=trust_claims)
        claimed_roles = payload.get("roles") if trust_claims else None
        return {
            "user_id": identity["user_id"],
            "org_id": identity["org_id"],
            "email": payload.get("email", ""),
            # A trusted token's own roles claim (native file-server
            # tokens, i.e. issuer == "fileserver" explicitly) always
            # wins — it's an explicit grant. Any other token (bridged,
            # or missing/unrecognized issuer) falls through to the live
            # org-role lookup, then the blanket default if that found
            # nothing either.
            "roles": claimed_roles or identity.get("roles") or DEFAULT_BRIDGED_ROLES,
            "source": "native",
        }
    except HTTPException:
        pass

    # Try Duniverse token
    try:
        payload = decode_duniverse_token(token)
        trust_claims = payload.get("issuer") == "fileserver"
        identity = await resolve_identity(payload, trust_claims=trust_claims)
        claimed_roles = payload.get("roles") if trust_claims else None
        return {
            "user_id": identity["user_id"],
            "org_id": identity["org_id"],
            "email": payload.get("email", ""),
            "roles": claimed_roles or identity.get("roles") or DEFAULT_BRIDGED_ROLES,
            "source": "duniverse",
        }
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Password hashing (presentation-link password protection) ───────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/foreign hash — fail closed, not an exception.
        return False


# ── Short-lived presentation-link unlock tokens ─────────────────────────
# Scoped tightly to one link's token so an unlock JWT for link A can never
# be replayed against link B — the resolve endpoint checks link_token
# against the URL's own {token} path param, not just signature validity.

_UNLOCK_TOKEN_SCOPE = "presentation_unlock"
_UNLOCK_TOKEN_TTL = timedelta(minutes=5)


def create_unlock_token(link_token: str) -> str:
    expire = datetime.utcnow() + _UNLOCK_TOKEN_TTL
    return jwt.encode(
        {"scope": _UNLOCK_TOKEN_SCOPE, "link_token": link_token, "exp": expire},
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )


def verify_unlock_token(unlock_token: str, link_token: str) -> bool:
    try:
        payload = jwt.decode(unlock_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return False
    return payload.get("scope") == _UNLOCK_TOKEN_SCOPE and payload.get("link_token") == link_token


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
