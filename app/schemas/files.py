from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime


class AssetResponse(BaseModel):
    id: str
    name: str
    asset_type: str
    source_type: str
    status: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    summary: Optional[str] = None
    tags: List[str] = []
    metadata: Dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime


class AssetListResponse(BaseModel):
    assets: List[AssetResponse]
    total: int
    limit: int
    offset: int


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    mode: str = "hybrid"
    asset_types: Optional[List[str]] = None
    project_id: Optional[str] = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    filters: Optional[Dict[str, Any]] = None
