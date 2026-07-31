"""
Asset repository — all DB operations for assets.
Production-grade with proper error handling, pagination, filtering.
"""
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import select, update, delete, func, and_, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.asset import Asset, AssetStatus, AssetType


class AssetRepository:

    async def create(self, db: AsyncSession, data: Dict[str, Any]) -> Asset:
        asset = Asset(
            id=uuid.UUID(data["id"]) if data.get("id") else uuid.uuid4(),
            organization_id=uuid.UUID(data["organization_id"]),
            project_id=uuid.UUID(data["project_id"]) if data.get("project_id") else None,
            parent_id=uuid.UUID(data["parent_id"]) if data.get("parent_id") else None,
            created_by=uuid.UUID(data["created_by"]),
            name=data["name"],
            asset_type=data["asset_type"],
            source_type=data.get("source_type", "upload"),
            status=data.get("status", AssetStatus.PENDING),
            blob_ref=data.get("blob_ref"),
            blob_bucket=data.get("blob_bucket"),
            mime_type=data.get("mime_type"),
            size_bytes=data.get("size_bytes"),
            checksum=data.get("checksum"),
            extra_data=data.get("extra_data", {}),
            tags=data.get("tags", []),
            external_id=data.get("external_id"),
            external_url=data.get("external_url"),
        )
        db.add(asset)
        await db.flush()
        await db.refresh(asset)
        return asset

    async def get_by_id(self, db: AsyncSession, asset_id: str, org_id: str) -> Optional[Asset]:
        try:
            asset_uuid = uuid.UUID(asset_id)
            org_uuid = uuid.UUID(org_id)
        except (ValueError, AttributeError):
            return None
        result = await db.execute(
            select(Asset).where(
                and_(
                    Asset.id == asset_uuid,
                    Asset.organization_id == org_uuid,
                    Asset.deleted_at.is_(None),
                )
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        db: AsyncSession,
        org_id: str,
        project_id: Optional[str] = None,
        asset_type: Optional[str] = None,
        parent_id: Optional[str] = None,
        parent_id_is_null: bool = False,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Asset], int]:
        try:
            org_uuid = uuid.UUID(org_id)
        except (ValueError, AttributeError):
            return [], 0
        filters = [
            Asset.organization_id == org_uuid,
            Asset.deleted_at.is_(None),
        ]
        if project_id:
            try:
                filters.append(Asset.project_id == uuid.UUID(project_id))
            except (ValueError, AttributeError):
                pass
        if asset_type:
            filters.append(Asset.asset_type == asset_type)
        if parent_id:
            filters.append(Asset.parent_id == uuid.UUID(parent_id))
        elif parent_id_is_null:
            filters.append(Asset.parent_id.is_(None))
        if status:
            filters.append(Asset.status == status)

        # Count
        count_result = await db.execute(
            select(func.count()).select_from(Asset).where(and_(*filters))
        )
        total = count_result.scalar()

        # Fetch
        result = await db.execute(
            select(Asset)
            .where(and_(*filters))
            .order_by(Asset.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        assets = result.scalars().all()
        return list(assets), total

    async def update_status(
        self,
        db: AsyncSession,
        asset_id: str,
        status: AssetStatus,
        extra_data: Optional[Dict] = None,
    ) -> Optional[Asset]:
        values = {"status": status, "updated_at": datetime.utcnow()}
        if status == AssetStatus.READY:
            values["processed_at"] = datetime.utcnow()
        if extra_data:
            values["extra_data"] = extra_data

        await db.execute(
            update(Asset)
            .where(Asset.id == uuid.UUID(asset_id))
            .values(**values)
        )
        await db.flush()
        result = await db.execute(select(Asset).where(Asset.id == uuid.UUID(asset_id)))
        return result.scalar_one_or_none()

    async def update_blob(
        self,
        db: AsyncSession,
        asset_id: str,
        blob_ref: str,
        blob_bucket: str,
        checksum: Optional[str] = None,
        size_bytes: Optional[int] = None,
    ) -> Optional[Asset]:
        values = {
            "blob_ref": blob_ref,
            "blob_bucket": blob_bucket,
            "status": AssetStatus.PROCESSING,
            "updated_at": datetime.utcnow(),
        }
        if checksum:
            values["checksum"] = checksum
        if size_bytes:
            values["size_bytes"] = size_bytes

        await db.execute(
            update(Asset).where(Asset.id == uuid.UUID(asset_id)).values(**values)
        )
        await db.flush()
        result = await db.execute(select(Asset).where(Asset.id == uuid.UUID(asset_id)))
        return result.scalar_one_or_none()

    async def soft_delete(self, db: AsyncSession, asset_id: str, org_id: str) -> bool:
        result = await db.execute(
            update(Asset)
            .where(
                and_(
                    Asset.id == uuid.UUID(asset_id),
                    Asset.organization_id == uuid.UUID(org_id),
                )
            )
            .values(status=AssetStatus.DELETED, deleted_at=datetime.utcnow())
        )
        return result.rowcount > 0

    async def search_fulltext(
        self,
        db: AsyncSession,
        org_id: str,
        query: str,
        asset_types: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Tuple[List[Asset], int]:
        try:
            org_uuid = uuid.UUID(org_id)
        except (ValueError, AttributeError):
            return [], 0
        filters = [
            Asset.organization_id == org_uuid,
            Asset.deleted_at.is_(None),
            or_(
                Asset.name.ilike(f"%{query}%"),
                Asset.summary.ilike(f"%{query}%"),
            )
        ]
        if asset_types:
            filters.append(Asset.asset_type.in_(asset_types))

        count_result = await db.execute(
            select(func.count()).select_from(Asset).where(and_(*filters))
        )
        total = count_result.scalar()

        result = await db.execute(
            select(Asset)
            .where(and_(*filters))
            .order_by(Asset.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total


    async def get_by_id_only(self, db: AsyncSession, asset_id: str) -> Optional[Asset]:
        """Get asset by ID only — no org check. Used for Duniverse token fallback."""
        try:
            asset_uuid = uuid.UUID(asset_id)
        except (ValueError, AttributeError):
            return None
        result = await db.execute(
            select(Asset).where(
                and_(
                    Asset.id == asset_uuid,
                    Asset.deleted_at.is_(None),
                )
            )
        )
        return result.scalar_one_or_none()


asset_repo = AssetRepository()
