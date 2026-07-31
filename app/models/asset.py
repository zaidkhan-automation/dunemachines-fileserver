"""
Core Asset model — everything is an asset.
Files, repos, messages, conversations, summaries, issues, PRs.
"""
import uuid
from datetime import datetime
from enum import Enum
from sqlalchemy import Column, String, Integer, BigInteger, Boolean, DateTime, JSON, ForeignKey, Text, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.core.database import Base


class AssetType(str, Enum):
    FILE = "file"
    FOLDER = "folder"
    REPO = "repo"
    MESSAGE = "message"
    CONVERSATION = "conversation"
    SUMMARY = "summary"
    ISSUE = "issue"
    PR = "pr"
    NOTE = "note"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    CODE = "code"
    DATASET = "dataset"


class SourceType(str, Enum):
    UPLOAD = "upload"
    GITHUB = "github"
    GDRIVE = "gdrive"
    SLACK = "slack"
    NOTION = "notion"
    LOCAL = "local"
    AI_GENERATED = "ai_generated"
    OMNIUS = "omnius"


class AssetStatus(str, Enum):
    PENDING = "pending"       # Upload initiated, not complete
    UPLOADING = "uploading"   # Upload in progress
    PROCESSING = "processing" # Workers running (OCR, embed, thumb)
    READY = "ready"           # Fully processed, available
    FAILED = "failed"         # Processing failed
    DELETED = "deleted"       # Soft deleted


class Asset(Base):
    __tablename__ = "assets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True, index=True)
    created_by = Column(UUID(as_uuid=True), nullable=False)

    # Asset identity
    name = Column(String(512), nullable=False)
    asset_type = Column(String(50), nullable=False, index=True)
    source_type = Column(String(50), nullable=False, default=SourceType.UPLOAD)
    status = Column(String(50), nullable=False, default=AssetStatus.PENDING, index=True)

    # Blob reference — points to object storage
    blob_ref = Column(String(1024), nullable=True)      # MinIO object key
    blob_bucket = Column(String(256), nullable=True)
    mime_type = Column(String(256), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    checksum = Column(String(128), nullable=True)       # SHA-256
    
    # Rich metadata — flexible JSONB
    extra_data = Column(JSONB, nullable=False, default=dict)
    # Examples:
    # FILE: {width, height, duration, pages, language}
    # REPO: {stars, language, default_branch, clone_url}
    # MESSAGE: {channel_id, thread_id, reactions}
    # SUMMARY: {source_asset_id, model, tokens}

    # AI-generated fields
    summary = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=False, default=list)
    
    # Version tracking
    version = Column(Integer, nullable=False, default=1)
    version_of = Column(UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True)

    # External source references
    external_id = Column(String(512), nullable=True)    # GitHub file SHA, GDrive ID etc
    external_url = Column(String(2048), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)


    __table_args__ = (
        Index("idx_assets_org_type", "organization_id", "asset_type"),
        Index("idx_assets_org_status", "organization_id", "status"),
        Index("idx_assets_project", "project_id", "asset_type"),
        Index("idx_assets_blob", "blob_ref"),
        Index("idx_assets_external", "external_id", "source_type"),
    )

    def __repr__(self):
        return f"<Asset {self.id} {self.name} ({self.asset_type})>"
