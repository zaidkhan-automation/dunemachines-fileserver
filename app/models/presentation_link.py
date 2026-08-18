"""
Presentation Link model — public shareable URLs for markdown decks.

created_by is intentionally a bare UUID column with no FK to `users`:
same convention as Asset.created_by / Version.created_by. The `users`
table has effectively never been populated (1 row, vs 31 distinct
asset creators with no matching row) — Duniverse-bridged identities
(the overwhelming majority of real traffic, see
app/core/security.py::resolve_identity) never get inserted there. A
hard FK on created_by would reject nearly every real create-link call.
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.core.database import Base


class PresentationLink(Base):
    __tablename__ = "presentation_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    file_id = Column(UUID(as_uuid=True), ForeignKey("assets.id"), nullable=False)
    created_by = Column(UUID(as_uuid=True), nullable=False)

    token = Column(String(32), unique=True, nullable=False)
    mode = Column(String(20), nullable=False)  # "live" | "snapshot"
    revision_id = Column(UUID(as_uuid=True), ForeignKey("versions.id"), nullable=True)

    title = Column(String(255), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    password_hash = Column(String(255), nullable=True)
    options = Column(JSONB, nullable=False, default=dict)

    access_count = Column(Integer, nullable=False, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_presentation_links_file", "file_id", "created_by"),
        Index("idx_presentation_links_expires", "expires_at"),
    )

    def __repr__(self):
        return f"<PresentationLink {self.id} token={self.token} mode={self.mode}>"
