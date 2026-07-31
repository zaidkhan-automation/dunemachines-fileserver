import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, BigInteger
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.core.database import Base

class Organization(Base):
    __tablename__ = "organizations"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(256), nullable=False)
    slug = Column(String(256), nullable=False, unique=True)
    owner_id = Column(UUID(as_uuid=True), nullable=False)
    storage_quota_bytes = Column(BigInteger, default=10 * 1024 * 1024 * 1024)
    storage_used_bytes = Column(BigInteger, default=0)
    is_active = Column(Boolean, default=True)
    extra_data = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
