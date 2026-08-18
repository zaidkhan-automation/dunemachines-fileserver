"""
Pydantic request/response models for the presentation-links feature.

Kept alongside the other request/response schemas (app/schemas/*.py);
the SQLAlchemy ORM model lives in app/models/presentation_link.py,
matching this codebase's existing models/ (ORM) vs schemas/ (Pydantic)
split (see Asset vs AssetResponse).
"""
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Literal

from pydantic import BaseModel, Field, field_validator


class LinkOptions(BaseModel):
    hideSpeakerNotes: bool = False
    allowDownload: bool = False
    startSlide: int = Field(default=0, ge=0)


class CreateLinkRequest(BaseModel):
    label: Optional[str] = Field(default=None, max_length=255)
    expiresAt: Optional[datetime] = None
    password: Optional[str] = Field(default=None, min_length=1, max_length=256)
    mode: Literal["live", "snapshot"] = "live"
    options: LinkOptions = Field(default_factory=LinkOptions)

    @field_validator("expiresAt")
    @classmethod
    def validate_future(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            # Accept both naive and tz-aware input; compare in UTC, then
            # normalize to a naive UTC datetime before it goes anywhere
            # else. presentation_links.expires_at is TIMESTAMP WITHOUT
            # TIME ZONE (matching every other timestamp column in this
            # codebase, all populated via naive datetime.utcnow()) —
            # asyncpg raises DataError on a tz-aware value there (confirmed
            # live: a client-sent "...Z" ISO string parses to a tz-aware
            # datetime and 500'd the create-link endpoint before this fix).
            if v.tzinfo is not None:
                now = datetime.now(v.tzinfo)
                if v <= now:
                    raise ValueError("expiresAt must be in the future")
                v = v.astimezone(timezone.utc).replace(tzinfo=None)
            elif v <= datetime.utcnow():
                raise ValueError("expiresAt must be in the future")
        return v


class PresentationLinkResponse(BaseModel):
    id: str
    token: str
    url: str
    fileId: str
    mode: str
    revisionId: Optional[str] = None
    title: Optional[str] = None
    expiresAt: Optional[datetime] = None
    createdAt: datetime
    accessCount: int
    options: Dict[str, Any]


class PresentationLinkListItem(BaseModel):
    id: str
    token: str
    url: str
    mode: str
    expiresAt: Optional[datetime] = None
    accessCount: int
    revokedAt: Optional[datetime] = None
    createdAt: datetime


class ResolvedPresentationResponse(BaseModel):
    title: Optional[str] = None
    markdown: str
    fileName: str
    revisionId: Optional[str] = None
    mode: str
    publishedAt: datetime
    options: Dict[str, Any]


class UnlockRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256)


class UnlockResponse(BaseModel):
    success: bool
    token: str


class RevokeResponse(BaseModel):
    success: bool
    revokedAt: datetime
