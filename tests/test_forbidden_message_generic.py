"""Finding #11: 403s must not leak which resource:action was checked."""
import pytest
from fastapi import HTTPException

from app.services.permissions.rbac import require_permission, require_any_permission


@pytest.mark.asyncio
async def test_require_permission_403_is_generic():
    check_fn = require_permission("assets", "delete")

    viewer = {"roles": ["viewer"], "org_id": "org-1", "user_id": "u1"}
    with pytest.raises(HTTPException) as exc_info:
        await check_fn(user=viewer)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Forbidden"
    assert "assets" not in exc_info.value.detail
    assert "delete" not in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_any_permission_403_is_generic():
    check_fn = require_any_permission(("assets", "delete"), ("org", "billing"))

    viewer = {"roles": ["viewer"], "org_id": "org-1", "user_id": "u1"}
    with pytest.raises(HTTPException) as exc_info:
        await check_fn(user=viewer)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Forbidden"
