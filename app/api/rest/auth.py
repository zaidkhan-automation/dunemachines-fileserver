from datetime import timedelta
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from app.core.security import create_access_token, get_current_user, resolve_identity, DEFAULT_BRIDGED_ROLES

# Scoped to this exchange only (not the global ACCESS_TOKEN_EXPIRE_MINUTES
# default, which other issuers here rely on for legitimately long-lived
# agent/collaborator tokens). org_id is a snapshot taken at exchange time —
# switching org afterward doesn't touch already-issued tokens, so a long
# expiry here directly means a switch can take up to that long to be
# reflected in what a user sees browsing files.
BRIDGED_TOKEN_EXPIRE = timedelta(minutes=15)

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    duniverse_token: str  # Exchange Duniverse token for file server token


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/token", response_model=TokenResponse)
async def exchange_token(req: TokenRequest):
    """
    Exchange a Duniverse JWT for a file server token.
    This allows Duniverse users to access the file server.
    """
    from app.core.security import decode_duniverse_token
    try:
        payload = decode_duniverse_token(req.duniverse_token)
        if not (payload.get("sub") or payload.get("user_id")):
            raise HTTPException(status_code=401, detail="Invalid Duniverse token")
        identity = resolve_identity(payload)

        token = create_access_token({
            "sub": identity["user_id"],
            "email": payload.get("email", ""),
            "org_id": identity["org_id"],
            "roles": payload.get("roles") or DEFAULT_BRIDGED_ROLES,
            "source": "duniverse",
        }, expires_delta=BRIDGED_TOKEN_EXPIRE)
        return TokenResponse(
            access_token=token,
            expires_in=int(BRIDGED_TOKEN_EXPIRE.total_seconds()),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    """Get current user info."""
    return user
