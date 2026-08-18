"""add_presentation_links_created_by_index

Revision ID: ca40a21382cf
Revises: 8c6816d35a0f
Create Date: 2026-08-18 19:01:39.953673

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ca40a21382cf'
down_revision: Union[str, None] = '8c6816d35a0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Covers presentation_link_repo.count_created_since (the per-user
    # create-link hourly-quota check, run on EVERY create-link request) —
    # confirmed via EXPLAIN this was a Seq Scan with no matching index.
    # Negligible today with a near-empty table, but this query runs on
    # every single create request, so it's the one cost here that grows
    # with total link volume rather than staying O(1) like the rest of
    # create_link's DB work.
    op.create_index(
        "idx_presentation_links_created_by_created_at",
        "presentation_links", ["created_by", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_presentation_links_created_by_created_at", table_name="presentation_links")
