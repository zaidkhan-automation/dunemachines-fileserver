"""add_presentation_links_organization_id

Revision ID: f3a91c7e2b04
Revises: ca40a21382cf
Create Date: 2026-08-24 00:00:00.000000

Adds a denormalized organization_id to presentation_links so a link's org
can be resolved without going through its (possibly soft-deleted) asset —
revoke_presentation_link previously 404'd on any link whose file had
already been deleted, since it resolved org via asset_repo.get_by_id
(deleted_at IS NULL filter), leaving no API-level way to revoke a link
once its source file was gone. Backfilled from assets.organization_id via
file_id, then set NOT NULL.

Also revokes existing links whose asset is already soft-deleted — a live
security finding, not just a schema change: 3 real "snapshot"-mode links
in production currently still serve their file's full content via
GET /p/{token} after the source file was deleted, because
presentation_service.resolve_link's snapshot branch never checked asset
existence (only the frozen Version row). Going forward this is closed by
DELETE /assets/{id} cascading a revoke onto the file's links (see
app/api/rest/files.py); this migration is the one-time cleanup for links
that predate that fix.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'f3a91c7e2b04'
down_revision: Union[str, None] = 'ca40a21382cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "presentation_links",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.execute("""
        UPDATE presentation_links pl
        SET organization_id = a.organization_id
        FROM assets a
        WHERE a.id = pl.file_id
    """)

    op.alter_column("presentation_links", "organization_id", nullable=False)

    op.create_index(
        "idx_presentation_links_organization_id",
        "presentation_links", ["organization_id"], unique=False,
    )

    # One-time backfill: revoke links whose source asset was already
    # soft-deleted before this fix existed (see docstring above).
    op.execute("""
        UPDATE presentation_links pl
        SET revoked_at = now(), updated_at = now()
        FROM assets a
        WHERE a.id = pl.file_id
          AND a.deleted_at IS NOT NULL
          AND pl.revoked_at IS NULL
    """)


def downgrade() -> None:
    op.drop_index("idx_presentation_links_organization_id", table_name="presentation_links")
    op.drop_column("presentation_links", "organization_id")
