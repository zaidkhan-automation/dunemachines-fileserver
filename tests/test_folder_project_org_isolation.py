"""Cross-org REST isolation, spot-checked beyond uploads (which
tests/test_upload_completion_ownership.py already proves end-to-end for
the upload-completion path specifically). Two real orgs, real DB,
disposable data — folders (list_folder_contents) and projects
(list_projects / get_project), the two highest-value endpoints the
production remediation audit named as unexercised.

Written for the audit's own test-quality gap finding: query-level org
scoping was already read and confirmed correct in both
asset_repo.get_by_id/list and project_repo.get_by_id/list (real WHERE
organization_id = :org_id clauses, not app-level afterthought checks) —
these tests prove that holds under a real two-org round trip, not just
by reading the SQL.

Calls the route functions directly (same reasoning as
tests/test_asset_content_endpoint.py / test_upload_completion_ownership.py:
TestClient's event-loop portal doesn't mix safely with a real async DB
connection created outside it).
"""
import uuid

import pytest

from app.api.rest.folders import list_folder_contents
from app.api.rest.projects import list_projects, get_project
from app.core.database import AsyncSessionLocal
from app.repositories.asset_repo import asset_repo
from app.repositories.project_repo import project_repo
from app.models.asset import AssetStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_folder(db, org_id, name="private folder"):
    return await asset_repo.create(db, {
        "organization_id": org_id,
        "created_by": str(uuid.uuid4()),
        "name": name,
        "asset_type": "folder",
        "status": AssetStatus.READY,
    })


async def _make_child_asset(db, org_id, parent_id, name="secret.txt"):
    return await asset_repo.create(db, {
        "organization_id": org_id,
        "created_by": str(uuid.uuid4()),
        "name": name,
        "asset_type": "document",
        "status": AssetStatus.READY,
        "parent_id": parent_id,
    })


def _user(org_id: str) -> dict:
    return {"user_id": str(uuid.uuid4()), "org_id": org_id, "roles": ["editor"]}


async def test_folder_contents_404_for_wrong_org():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        folder = await _make_folder(db, org_a)
        child = await _make_child_asset(db, org_a, str(folder.id))
        await db.commit()
        try:
            # Owner org can list it. limit/offset passed explicitly —
            # calling the route function directly bypasses FastAPI's
            # Query(...) dependency resolution, so its defaults would
            # otherwise arrive as Query objects, not ints.
            own = await list_folder_contents(str(folder.id), limit=50, offset=0, db=db, user=_user(org_a))
            assert own["total"] == 1
            assert own["items"][0]["id"] == str(child.id)

            # A different org gets a clean 404, not an empty-but-200 list
            # (which would still confirm the folder_id exists).
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await list_folder_contents(str(folder.id), limit=50, offset=0, db=db, user=_user(org_b))
            assert exc_info.value.status_code == 404
        finally:
            await asset_repo.hard_delete(db, str(child.id)) if hasattr(asset_repo, "hard_delete") else None
            from sqlalchemy import delete
            from app.models.asset import Asset
            await db.execute(delete(Asset).where(Asset.id.in_([folder.id, child.id])))
            await db.commit()


async def test_project_list_and_get_never_cross_org():
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        project = await project_repo.create(db, {
            "organization_id": org_a,
            "name": "org A's confidential project",
            "created_by": str(uuid.uuid4()),
        })
        await db.commit()
        try:
            # list_projects for org B must never include org A's project.
            listing_b = await list_projects(limit=50, offset=0, db=db, user=_user(org_b))
            b_ids = {p["id"] for p in listing_b["projects"]}
            assert str(project.id) not in b_ids

            listing_a = await list_projects(limit=50, offset=0, db=db, user=_user(org_a))
            a_ids = {p["id"] for p in listing_a["projects"]}
            assert str(project.id) in a_ids

            # Direct get_project by ID, from the wrong org, must 404 —
            # not leak the project under a "not a member" style error
            # that would confirm existence.
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                await get_project(str(project.id), db=db, user=_user(org_b))
            assert exc_info.value.status_code == 404

            own = await get_project(str(project.id), db=db, user=_user(org_a))
            assert own["id"] == str(project.id)
        finally:
            from sqlalchemy import delete
            from app.models.project import Project
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.commit()
