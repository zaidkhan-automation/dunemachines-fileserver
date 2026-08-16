"""
Regression tests for the omnius_backend-bridged-token trust boundary in
get_current_user / resolve_identity (app/core/security.py).

Context: dunemachines_backend and this service share JWT_SECRET so that
fileserver_sync.mint_fileserver_token() can mint agent-sync tokens
without a separate auth flow. A token decoding successfully against
that shared secret was NOT proof it was self-issued by this service —
before this fix, get_current_user trusted such a token's own
roles/org_id claims unconditionally, so anyone able to mint a token
with the shared secret could claim owner-level access to any org
(confirmed live during a production RBAC audit).

Fix: only "issuer": "fileserver" (or absent — genuinely native) tokens
get their claims trusted. "issuer": "omnius_backend" tokens must always
re-derive role/org_id from the live DB lookup (get_org_role /
get_active_org_id), which is itself fail-open on a DB outage but never
trusts the claim as a substitute.

These tests call the security module directly and mock the live-lookup
functions — no DB/Redis dependency, no app startup required.
"""
import time
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token, get_current_user

FORGED_ORG_ID = "00000000-0000-0000-0000-00000000dead"


def _mint_raw(payload: dict) -> str:
    to_encode = {**payload, "iat": int(time.time()), "exp": int(time.time()) + 600}
    return pyjwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


class _Creds:
    def __init__(self, token):
        self.credentials = token


@pytest.mark.asyncio
async def test_forged_bridged_token_ignores_roles_and_org_id_claims():
    """A token minted with the shared secret, issuer=omnius_backend,
    claiming owner of an org the user isn't a member of — must not be
    granted those claims. No live membership exists for this throwaway
    uid/org pair, so it must fall through to DEFAULT_BRIDGED_ROLES and
    a derived (not the forged) org_id. This is the exact shape of the
    live PoC that found the bug."""
    token = _mint_raw({
        "sub": "78999",
        "uid": 78999,
        "issuer": "omnius_backend",
        "roles": ["owner"],
        "org_id": FORGED_ORG_ID,
    })

    with patch("app.core.omnius_client.get_active_org_id", new=AsyncMock(return_value=None)), \
         patch("app.core.omnius_client.get_org_role", new=AsyncMock(return_value=None)):
        user = await get_current_user(_Creds(token))

    assert user["roles"] != ["owner"], "forged owner role claim must not be trusted for a bridged token"
    assert user["org_id"] != FORGED_ORG_ID, "forged org_id claim must not be trusted for a bridged token"
    assert user["roles"] == ["editor"]  # DEFAULT_BRIDGED_ROLES fallback


@pytest.mark.asyncio
async def test_real_bridged_token_still_gets_correct_access_via_live_lookup():
    """Same issuer=omnius_backend shape, but this time the live DB
    lookup finds a real admin membership for (uid, org_id) — proves the
    fix didn't just lock bridged tokens out, it correctly re-derives
    real access from the DB instead of the token's own (here,
    deliberately wrong) claim."""
    real_org_id = "11111111-1111-1111-1111-111111111111"
    token = _mint_raw({
        "sub": "78998",
        "uid": 78998,
        "issuer": "omnius_backend",
        "roles": ["editor"],  # deliberately wrong vs. the live "admin" below
        "org_id": real_org_id,
    })

    with patch("app.core.omnius_client.get_active_org_id", new=AsyncMock(return_value=real_org_id)), \
         patch("app.core.omnius_client.get_org_role", new=AsyncMock(return_value="admin")):
        user = await get_current_user(_Creds(token))

    assert user["org_id"] == real_org_id
    assert user["roles"] == ["admin"], "must reflect the live DB role, not the token's own roles claim"


@pytest.mark.asyncio
async def test_native_fileserver_token_trusts_its_own_claims():
    """A token minted by this service's own create_access_token (e.g.
    the /auth/token exchange, or an agent/collaborator token) — its
    roles/org_id claims are self-issued after a live check at mint
    time, so it's fine (and necessary — no DB row backs an
    agent/collaborator token) to trust them directly."""
    org_id = "22222222-2222-2222-2222-222222222222"
    token = create_access_token({
        "sub": "agent:ci-bot",
        "org_id": org_id,
        "roles": ["editor"],
    })

    user = await get_current_user(_Creds(token))

    assert user["org_id"] == org_id
    assert user["roles"] == ["editor"]
