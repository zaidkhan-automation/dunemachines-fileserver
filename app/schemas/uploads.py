from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime


class InitUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)
    mime_type: str = Field(..., min_length=1, max_length=256)
    size_bytes: int = Field(..., gt=0, le=500 * 1024 * 1024)  # 500MB max
    project_id: Optional[str] = None
    asset_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class InitUploadResponse(BaseModel):
    upload_id: str
    signed_url: str
    object_key: str
    expires_at: str
    asset: Dict[str, Any]


class CompleteUploadRequest(BaseModel):
    object_key: str
    checksum: Optional[str] = None


class CompleteUploadResponse(BaseModel):
    asset_id: str
    status: str
    message: str
