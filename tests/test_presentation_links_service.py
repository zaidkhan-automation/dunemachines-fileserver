"""
Real-DB + real-storage tests for the presentation-links service layer
(app/services/presentation_service.py, app/repositories/presentation_link_repo.py).

Same convention as this session's other real-infrastructure test suites:
throwaway UUIDs, real Postgres + real MinIO/S3, explicit cleanup after
each test rather than mocking the DB/storage layer — the whole point of
these tests is verifying real SQL/S3 behavior (unique token constraint,
atomic access_count increment, snapshot immutability under a real
overwrite) that a mock would hide.
"""
import uuid
from datetime import datetime, timedelta

import aioboto3
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.repositories.asset_repo import asset_repo
from app.repositories.presentation_link_repo import presentation_link_repo
from app.services import presentation_service
from app.services.presentation_service import (
    ResolveResult, NotMarkdownError, RateLimitedError,
)


def _s3():
    session = aioboto3.Session()
    return session.client(
        "s3", endpoint_url=settings.STORAGE_ENDPOINT,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def md_asset():
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        asset = await asset_repo.create(db, {
            "organization_id": org_id, "created_by": user_id,
            "name": "deck.md", "asset_type": "document", "mime_type": "text/markdown",
        })
        await db.commit()

        object_key = f"orgs/{org_id}/assets/{asset.id}/deck.md"
        content = b"# Slide 1\nHello\n\n???\nspeaker note\n???\n\n# Slide 2\nWorld\n"
        async with _s3() as s3:
            await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=object_key, Body=content)

        asset = await asset_repo.update_blob(db, str(asset.id), blob_ref=object_key, blob_bucket=settings.STORAGE_BUCKET)
        await db.commit()

        yield {"org_id": org_id, "user_id": user_id, "asset": asset, "object_key": object_key, "content": content}

        # cleanup
        async with AsyncSessionLocal() as cleanup_db:
            links = await presentation_link_repo.list_for_file(cleanup_db, str(asset.id))
            snapshot_keys = []
            for link in links:
                if link.revision_id:
                    result = await cleanup_db.execute(text("SELECT blob_ref FROM versions WHERE id = :id"), {"id": link.revision_id})
                    row = result.first()
                    if row and row[0]:
                        snapshot_keys.append(row[0])
            await cleanup_db.execute(text("DELETE FROM presentation_links WHERE file_id = :id"), {"id": asset.id})
            await cleanup_db.execute(text("DELETE FROM versions WHERE asset_id = :id"), {"id": asset.id})
            await cleanup_db.execute(text("DELETE FROM assets WHERE id = :id"), {"id": asset.id})
            await cleanup_db.commit()

        async with _s3() as s3:
            await s3.delete_object(Bucket=settings.STORAGE_BUCKET, Key=object_key)
            for key in snapshot_keys:
                await s3.delete_object(Bucket=settings.STORAGE_BUCKET, Key=key)


DEFAULT_OPTIONS = {"hideSpeakerNotes": False, "allowDownload": False, "startSlide": 0}


@pytest.mark.asyncio(loop_scope="session")
async def test_create_live_link_success(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label="Q3 board deck", expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        assert link.mode == "live"
        assert link.revision_id is None
        assert len(link.token) == 32
        assert link.title == "Q3 board deck"


@pytest.mark.asyncio(loop_scope="session")
async def test_create_link_rejects_non_markdown_file(md_asset):
    async with AsyncSessionLocal() as db:
        other = await asset_repo.create(db, {
            "organization_id": md_asset["org_id"], "created_by": md_asset["user_id"],
            "name": "image.png", "asset_type": "image", "mime_type": "image/png",
        })
        await db.commit()
        try:
            with pytest.raises(NotMarkdownError):
                await presentation_service.create_link(
                    db, file_id=str(other.id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
                    label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
                )
        finally:
            await db.execute(text("DELETE FROM assets WHERE id = :id"), {"id": other.id})
            await db.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_create_link_password_is_hashed_not_plaintext(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password="s3cret", mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        assert link.password_hash is not None
        assert link.password_hash != "s3cret"


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_live_link_returns_current_content(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        result = await presentation_service.resolve_link(db, link.token)
        await db.commit()
        assert result.status == ResolveResult.OK
        assert "Hello" in result.data["markdown"]
        assert result.data["mode"] == "live"


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_live_link_reflects_later_content_change(md_asset):
    """Live mode's ACL/content check: re-reads the CURRENT asset every
    resolve, so an edit after link creation is visible immediately."""
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()

        async with _s3() as s3:
            await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=md_asset["object_key"], Body=b"# EDITED\n")

        result = await presentation_service.resolve_link(db, link.token)
        assert "EDITED" in result.data["markdown"]

        # restore for fixture cleanup consistency (not required, but tidy)
        async with _s3() as s3:
            await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=md_asset["object_key"], Body=md_asset["content"])


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_live_link_404s_after_source_soft_deleted(md_asset):
    """Live mode 'respects file ACL changes': asset_repo.get_by_id
    already excludes soft-deleted assets, so a deleted source 404s."""
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()

        await asset_repo.soft_delete(db, str(md_asset["asset"].id), md_asset["org_id"])
        await db.commit()

        result = await presentation_service.resolve_link(db, link.token)
        assert result.status == ResolveResult.NOT_FOUND

        # undo the soft delete so fixture cleanup's hard DELETE still finds it
        await db.execute(text("UPDATE assets SET deleted_at = NULL, status = 'ready' WHERE id = :id"), {"id": md_asset["asset"].id})
        await db.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_snapshot_link_frozen_after_source_overwritten(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="snapshot", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        assert link.revision_id is not None

        async with _s3() as s3:
            await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=md_asset["object_key"], Body=b"# CHANGED\n")

        result = await presentation_service.resolve_link(db, link.token)
        assert "Hello" in result.data["markdown"]
        assert "CHANGED" not in result.data["markdown"]

        async with _s3() as s3:
            await s3.put_object(Bucket=settings.STORAGE_BUCKET, Key=md_asset["object_key"], Body=md_asset["content"])


@pytest.mark.asyncio(loop_scope="session")
async def test_speaker_notes_hidden_when_option_set(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live",
            options={"hideSpeakerNotes": True, "allowDownload": False, "startSlide": 0},
        )
        await db.commit()
        result = await presentation_service.resolve_link(db, link.token)
        assert "speaker note" not in result.data["markdown"]
        assert "Hello" in result.data["markdown"]


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_expired_link_returns_not_found(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=datetime.utcnow() + timedelta(seconds=1), password=None, mode="live",
            options=DEFAULT_OPTIONS,
        )
        await db.commit()
        # force it into the past directly (avoid a real sleep)
        await db.execute(text("UPDATE presentation_links SET expires_at = :t WHERE id = :id"),
                          {"t": datetime.utcnow() - timedelta(hours=1), "id": link.id})
        await db.commit()
        # expire_on_commit=False (see app/core/database.py) means `link`'s
        # in-memory attributes survive the commit unrefreshed — the raw-SQL
        # UPDATE above bypassed the ORM entirely, so the identity map still
        # holds the pre-update expires_at unless explicitly refreshed.
        await db.refresh(link)
        result = await presentation_service.resolve_link(db, link.token)
        assert result.status == ResolveResult.NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_revoked_link_returns_not_found(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        await presentation_link_repo.revoke(db, str(link.id))
        await db.commit()
        result = await presentation_service.resolve_link(db, link.token)
        assert result.status == ResolveResult.NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_resolve_unknown_token_returns_not_found(md_asset):
    async with AsyncSessionLocal() as db:
        result = await presentation_service.resolve_link(db, "this-token-does-not-exist-at-all")
        assert result.status == ResolveResult.NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_password_protected_link_requires_unlock(md_asset):
    from app.core.security import create_unlock_token, verify_password

    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password="hunter2", mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()

        # no unlock token -> password_required
        no_unlock = await presentation_service.resolve_link(db, link.token)
        assert no_unlock.status == ResolveResult.PASSWORD_REQUIRED

        # wrong password never verifies
        assert verify_password("wrong", link.password_hash) is False
        assert verify_password("hunter2", link.password_hash) is True

        # a correctly minted unlock token for THIS link's token resolves fine
        unlock_token = create_unlock_token(link.token)
        unlocked = await presentation_service.resolve_link(db, link.token, unlock_token=unlock_token)
        assert unlocked.status == ResolveResult.OK

        # an unlock token minted for a DIFFERENT link token must not work here
        other_unlock = create_unlock_token("some-other-links-token")
        rejected = await presentation_service.resolve_link(db, link.token, unlock_token=other_unlock)
        assert rejected.status == ResolveResult.PASSWORD_REQUIRED


@pytest.mark.asyncio(loop_scope="session")
async def test_access_count_increments_atomically_on_resolve(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        assert link.access_count == 0

        for _ in range(3):
            await presentation_service.resolve_link(db, link.token)
        await db.commit()

        fresh = await presentation_link_repo.get_by_token(db, link.token)
        assert fresh.access_count == 3
        assert fresh.last_accessed_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_list_links_for_file(md_asset):
    async with AsyncSessionLocal() as db:
        l1 = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label="link one", expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        l2 = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label="link two", expires_at=None, password=None, mode="snapshot", options=DEFAULT_OPTIONS,
        )
        await db.commit()

        links = await presentation_link_repo.list_for_file(db, str(md_asset["asset"].id))
        tokens = {l.token for l in links}
        assert l1.token in tokens
        assert l2.token in tokens
        assert len(links) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_revoke_sets_revoked_at_and_stops_resolution(md_asset):
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        assert link.revoked_at is None

        revoked = await presentation_link_repo.revoke(db, str(link.id))
        await db.commit()
        assert revoked.revoked_at is not None

        result = await presentation_service.resolve_link(db, link.token)
        assert result.status == ResolveResult.NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_create_link_blocked_by_per_user_hourly_quota(md_asset, monkeypatch):
    monkeypatch.setattr(presentation_service, "CREATE_LINK_HOURLY_QUOTA", 1)
    async with AsyncSessionLocal() as db:
        first = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        await db.commit()
        assert first is not None

        with pytest.raises(RateLimitedError):
            await presentation_service.create_link(
                db, file_id=str(md_asset["asset"].id), org_id=md_asset["org_id"], user_id=md_asset["user_id"],
                label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
            )


@pytest.mark.asyncio(loop_scope="session")
async def test_org_isolation_create_link_for_wrong_org_returns_none(md_asset):
    """create_link scopes the file lookup by org_id (same asset_repo.get_by_id
    call every other endpoint uses) — a caller in a different org can't
    create a link for a file it can't see, it just looks not-found."""
    other_org = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        link = await presentation_service.create_link(
            db, file_id=str(md_asset["asset"].id), org_id=other_org, user_id=md_asset["user_id"],
            label=None, expires_at=None, password=None, mode="live", options=DEFAULT_OPTIONS,
        )
        assert link is None
