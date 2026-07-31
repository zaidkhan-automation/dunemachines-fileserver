import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.core.database import Base

class Version(Base):
    __tablename__ = "versions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    organization_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    created_by = Column(UUID(as_uuid=True), nullable=False)
    version_number = Column(Integer, nullable=False)
    blob_ref = Column(String(1024), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    checksum = Column(String(128), nullable=True)
    change_summary = Column(Text, nullable=True)
    extra_data = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
