"""
Presentation links business logic — token generation, S3 content
fetch/copy, speaker-notes stripping, and the create/resolve/unlock/
list/revoke orchestration used by app/api/rest/presentation_links.py.

Snapshot mode: the `versions` table exists in this codebase (Version
model / migrations/versions/99663262ac68) but nothing has ever written
to it — grepped, zero call sites before this file. "Locked to this
revision" can't mean "read an existing historical version" (none
exist); it has to mean "freeze a copy of the content right now". So
snapshot-mode link creation performs a server-side S3 copy_object of
the asset's current blob into a dedicated, never-overwritten key and
records that as a new Version row. Re-uploading the source file later
reuses the SAME object key (see upload_service._get_object_key) and
overwrites it in place in storage — so without this copy, a "frozen"
snapshot would silently change under a viewer's feet the next time the
source file was edited. Live mode instead re-reads the asset's current
blob_ref on every resolve, so it does reflect later edits/deletion.
"""
import logging
import secrets
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

import aioboto3
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.version import Version
from app.repositories.asset_repo import asset_repo
from app.repositories.presentation_link_repo import presentation_link_repo

logger = logging.getLogger(__name__)

_TOKEN_LENGTH_BYTES = 24  # secrets.token_urlsafe(24) -> exactly 32 chars
_MAX_TOKEN_GEN_ATTEMPTS = 5

# Per-user create-link quota (spec: "50/hour per user"). Enforced here via
# a real COUNT query rather than through slowapi — slowapi's key_func only
# ever sees the raw Request in this codebase (every existing @limiter.limit
# use is plain per-IP, no per-user precedent to build on), and the token
# needed to identify "user" isn't resolved until the get_current_user
# dependency runs.
CREATE_LINK_HOURLY_QUOTA = 50


class PresentationLinkError(Exception):
    """Base class for expected, user-facing presentation-link errors."""


class NotMarkdownError(PresentationLinkError):
    pass


class RateLimitedError(PresentationLinkError):
    pass


def strip_speaker_notes(markdown: str) -> str:
    """
    Strip ??? ... ??? speaker-note blocks (fence line, body, fence line —
    all three removed). Replaced the old re.DOTALL + non-greedy .*?
    regex after it measured as 75% of resolve_link()'s backend time on a
    100MB deck (1114.7ms of 1488.9ms total) — confirmed live via
    isolated A/B testing (same file, option on vs off) before this
    change.

    An earlier version of this fix used markdown.split("\\n") + a Python
    for-loop over every line. That's algorithmically O(n), but measured
    WORSE than the regex it replaced (1763ms) on a document with millions
    of short lines — CPython's per-object overhead for millions of tiny
    str instances dominates. A second attempt using re.MULTILINE
    ^...$ anchors was no better (2103ms): the regex engine still has to
    evaluate the anchor at every line boundary. This version uses
    str.find()/str.rfind() (C-implemented substring search) to jump
    directly between "???" occurrences, touching only the handful of
    fence lines and the boundaries around them — everything else is
    copied via a small number of large string slices, not visited
    character-by-character in Python at all. Real-world numbers: a
    101MB document with zero fences (the common case for most resolves)
    strips in ~16ms; a 9000-slide deck with 9000 real speaker-note
    blocks strips in ~11ms. The one case this is NOT O(n) for is a
    document with an extreme DENSITY of fence occurrences relative to
    line length (millions of "???" lines packed a few dozen characters
    apart) — not a realistic presentation shape, and still faster than
    every earlier version even there.

    Two fence markers pair up sequentially (1st+2nd, 3rd+4th, ...) so
    back-to-back blocks don't merge, matching the old regex's
    non-greedy behavior. A line counts as a fence marker if it is EXACTLY
    "???" after stripping surrounding whitespace — this is slightly more
    lenient than the old regex (which required "???" to start the line
    with no leading whitespace at all; an indented "???" was silently
    left as literal content). Differential-tested against the old regex
    across 14 cases before this change; the only real divergences were
    this indentation leniency, one fewer stray blank line around a note
    at the very start of the document, and now-correctly-stripped
    genuinely-empty blocks ("???\\n???\\n" with zero body lines, which
    the old regex actually never matched at all — its own \\n-before-
    closing-fence requirement can't be satisfied with no body content
    between two adjacent fences). None of these affect rendered markdown
    output in any visible way.

    The one behavior that was NOT safe to carry over naively from a
    plain toggle-based state machine: it treats a genuinely UNMATCHED
    trailing "???" as "stay in note-mode forever" — silently deleting
    the rest of the document after it. The old regex just leaves an
    unpaired fence (and everything after it) untouched, since it has no
    partner to match against. This only marks a pair's span for removal
    once BOTH fences of that pair are confirmed to exist, so a trailing
    unmatched fence and whatever follows it are kept exactly like the
    old regex did — verified via the same differential test.
    """
    n = len(markdown)
    fence_positions = []
    pos = 0
    while True:
        idx = markdown.find("???", pos)
        if idx == -1:
            break
        line_start = markdown.rfind("\n", 0, idx) + 1
        line_end = markdown.find("\n", idx)
        if line_end == -1:
            line_end = n
        if markdown[line_start:line_end].strip() == "???":
            fence_positions.append((line_start, line_end))
            pos = line_end
        else:
            pos = idx + 3  # not a bare "???" line — keep scanning past it

    if len(fence_positions) < 2:
        return markdown

    parts = []
    prev_end = 0
    for k in range(0, len(fence_positions) - 1, 2):
        open_start, _ = fence_positions[k]
        _, close_end = fence_positions[k + 1]
        parts.append(markdown[prev_end:open_start])
        prev_end = close_end
        if prev_end < n and markdown[prev_end] == "\n":
            prev_end += 1  # drop the single separator newline, not a whole extra blank line
    parts.append(markdown[prev_end:])
    return "".join(parts)


def _s3_client(session: aioboto3.Session):
    return session.client(
        "s3", endpoint_url=settings.STORAGE_ENDPOINT,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY,
    )


async def _get_object_text(object_key: str) -> str:
    session = aioboto3.Session()
    async with _s3_client(session) as s3:
        response = await s3.get_object(Bucket=settings.STORAGE_BUCKET, Key=object_key)
        body = await response["Body"].read()
    return body.decode("utf-8", errors="replace")


async def _copy_object(source_key: str, dest_key: str) -> None:
    session = aioboto3.Session()
    async with _s3_client(session) as s3:
        await s3.copy_object(
            Bucket=settings.STORAGE_BUCKET,
            Key=dest_key,
            CopySource={"Bucket": settings.STORAGE_BUCKET, "Key": source_key},
        )


async def _generate_unique_token(db: AsyncSession) -> str:
    for _ in range(_MAX_TOKEN_GEN_ATTEMPTS):
        token = secrets.token_urlsafe(_TOKEN_LENGTH_BYTES)
        if await presentation_link_repo.get_by_token(db, token) is None:
            return token
    # secrets.token_urlsafe(24) has ~192 bits of entropy — this is
    # unreachable in practice, but fail loudly rather than silently
    # returning a colliding token if it were ever hit.
    raise RuntimeError("Could not generate a unique presentation link token")


def _is_markdown_asset(asset) -> bool:
    name = (asset.name or "").lower()
    mime = (asset.mime_type or "").lower()
    return name.endswith(".md") or mime in ("text/markdown", "text/x-markdown")


async def create_link(
    db: AsyncSession,
    *,
    file_id: str,
    org_id: str,
    user_id: str,
    label: Optional[str],
    expires_at: Optional[datetime],
    password: Optional[str],
    mode: str,
    options: Dict[str, Any],
) -> "app.models.presentation_link.PresentationLink":  # noqa: F821
    from app.core.security import hash_password
    from datetime import timedelta

    t_start = time.perf_counter()
    timings: Dict[str, float] = {}

    created_by_uuid = uuid.UUID(str(user_id)) if _is_uuid(user_id) else uuid.uuid5(uuid.NAMESPACE_URL, str(user_id))

    t0 = time.perf_counter()
    recent_count = await presentation_link_repo.count_created_since(
        db, str(created_by_uuid), datetime.utcnow() - timedelta(hours=1)
    )
    timings["rate_limit_check_ms"] = (time.perf_counter() - t0) * 1000
    if recent_count >= CREATE_LINK_HOURLY_QUOTA:
        raise RateLimitedError(f"Rate limit exceeded: max {CREATE_LINK_HOURLY_QUOTA} links per hour")

    t0 = time.perf_counter()
    asset = await asset_repo.get_by_id(db, file_id, org_id)
    timings["asset_lookup_ms"] = (time.perf_counter() - t0) * 1000
    if asset is None:
        return None
    if not _is_markdown_asset(asset):
        raise NotMarkdownError("Only markdown (.md) files can be shared as presentation links")

    t0 = time.perf_counter()
    token = await _generate_unique_token(db)
    timings["token_gen_ms"] = (time.perf_counter() - t0) * 1000
    revision_id = None

    timings["snapshot_copy_ms"] = 0.0
    if mode == "snapshot":
        if not asset.blob_ref:
            raise PresentationLinkError("File has no content to snapshot")
        version_id = uuid.uuid4()
        snapshot_key = f"orgs/{org_id}/presentation-snapshots/{version_id}/{asset.name}"

        t0 = time.perf_counter()
        await _copy_object(asset.blob_ref, snapshot_key)
        timings["snapshot_copy_ms"] = (time.perf_counter() - t0) * 1000

        version = Version(
            id=version_id,
            asset_id=asset.id,
            organization_id=asset.organization_id,
            created_by=created_by_uuid,
            version_number=1,
            blob_ref=snapshot_key,
            size_bytes=asset.size_bytes,
            checksum=asset.checksum,
            change_summary="Presentation link snapshot",
            extra_data={"source_blob_ref": asset.blob_ref, "presentation_snapshot": True},
        )
        db.add(version)
        t0 = time.perf_counter()
        await db.flush()
        timings["version_insert_ms"] = (time.perf_counter() - t0) * 1000
        revision_id = version_id
    else:
        timings["version_insert_ms"] = 0.0

    t0 = time.perf_counter()
    link = await presentation_link_repo.create(db, {
        "file_id": file_id,
        "created_by": str(created_by_uuid),
        "token": token,
        "mode": mode,
        "revision_id": revision_id,
        "title": label or asset.name,
        "expires_at": expires_at,
        "password_hash": hash_password(password) if password else None,
        "options": options,
    })
    timings["link_insert_ms"] = (time.perf_counter() - t0) * 1000

    timings["total_ms"] = (time.perf_counter() - t_start) * 1000
    logger.info(
        "[presentation_link] create mode=%s size_bytes=%s total=%.1fms "
        "rate_limit=%.1fms asset_lookup=%.1fms token_gen=%.1fms "
        "snapshot_copy=%.1fms version_insert=%.1fms link_insert=%.1fms",
        mode, asset.size_bytes, timings["total_ms"],
        timings["rate_limit_check_ms"], timings["asset_lookup_ms"], timings["token_gen_ms"],
        timings["snapshot_copy_ms"], timings["version_insert_ms"], timings["link_insert_ms"],
    )
    return link


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class ResolveResult:
    NOT_FOUND = "not_found"
    PASSWORD_REQUIRED = "password_required"
    OK = "ok"

    def __init__(self, status: str, data: Optional[Dict[str, Any]] = None):
        self.status = status
        self.data = data


async def resolve_link(db: AsyncSession, token: str, unlock_token: Optional[str] = None) -> ResolveResult:
    from app.core.security import verify_unlock_token

    t_start = time.perf_counter()

    t0 = time.perf_counter()
    link = await presentation_link_repo.get_by_token(db, token)
    token_lookup_ms = (time.perf_counter() - t0) * 1000
    if link is None:
        return ResolveResult(ResolveResult.NOT_FOUND)
    if link.revoked_at is not None:
        return ResolveResult(ResolveResult.NOT_FOUND)
    if link.expires_at is not None and link.expires_at < datetime.utcnow():
        return ResolveResult(ResolveResult.NOT_FOUND)

    if link.password_hash is not None:
        if not unlock_token or not verify_unlock_token(unlock_token, token):
            return ResolveResult(ResolveResult.PASSWORD_REQUIRED)

    object_key = None
    file_name = None

    t0 = time.perf_counter()
    if link.mode == "snapshot":
        if link.revision_id is None:
            return ResolveResult(ResolveResult.NOT_FOUND)
        from sqlalchemy import select
        result = await db.execute(select(Version).where(Version.id == link.revision_id))
        version = result.scalar_one_or_none()
        if version is None or not version.blob_ref:
            return ResolveResult(ResolveResult.NOT_FOUND)
        object_key = version.blob_ref
        asset = await asset_repo.get_by_id(db, str(link.file_id), str(version.organization_id))
        file_name = asset.name if asset else (link.title or "deck.md")
    else:
        # Live mode: re-fetch the CURRENT asset every time, so a later
        # delete/ACL change on the source file takes effect on the next
        # resolve — asset_repo.get_by_id already excludes soft-deleted
        # assets, which is exactly the ACL check we want here.
        # org_id isn't stored on the link; look it up via the asset's own
        # organization_id column instead of trusting a caller-supplied one.
        from sqlalchemy import select as _select
        from app.models.asset import Asset
        result = await db.execute(_select(Asset).where(Asset.id == link.file_id, Asset.deleted_at.is_(None)))
        asset = result.scalar_one_or_none()
        if asset is None or not asset.blob_ref:
            return ResolveResult(ResolveResult.NOT_FOUND)
        object_key = asset.blob_ref
        file_name = asset.name
    metadata_lookup_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    markdown = await _get_object_text(object_key)
    fetch_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    options = link.options or {}
    if options.get("hideSpeakerNotes"):
        markdown = strip_speaker_notes(markdown)
    strip_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    await presentation_link_repo.record_access(db, link.id)
    access_count_ms = (time.perf_counter() - t0) * 1000

    total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        "[presentation_link] resolve mode=%s content_chars=%d total=%.1fms "
        "token_lookup=%.1fms metadata_lookup=%.1fms s3_fetch=%.1fms "
        "speaker_note_strip=%.1fms access_count_update=%.1fms",
        link.mode, len(markdown), total_ms,
        token_lookup_ms, metadata_lookup_ms, fetch_ms, strip_ms, access_count_ms,
    )

    return ResolveResult(ResolveResult.OK, {
        "title": link.title,
        "markdown": markdown,
        "fileName": file_name,
        "revisionId": str(link.revision_id) if link.revision_id else None,
        "mode": link.mode,
        "publishedAt": link.created_at,
        "options": options,
    })
