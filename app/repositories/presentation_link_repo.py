"""
Presentation link repository — all DB operations for presentation_links.
Mirrors app/repositories/asset_repo.py's style.
"""
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.presentation_link import PresentationLink


class PresentationLinkRepository:

    async def create(self, db: AsyncSession, data: Dict[str, Any]) -> PresentationLink:
        link = PresentationLink(
            id=data.get("id") or uuid.uuid4(),
            file_id=uuid.UUID(str(data["file_id"])),
            created_by=uuid.UUID(str(data["created_by"])),
            token=data["token"],
            mode=data["mode"],
            revision_id=uuid.UUID(str(data["revision_id"])) if data.get("revision_id") else None,
            title=data.get("title"),
            expires_at=data.get("expires_at"),
            password_hash=data.get("password_hash"),
            options=data.get("options", {}),
        )
        db.add(link)
        await db.flush()
        await db.refresh(link)
        return link

    async def get_by_token(self, db: AsyncSession, token: str) -> Optional[PresentationLink]:
        result = await db.execute(select(PresentationLink).where(PresentationLink.token == token))
        return result.scalar_one_or_none()

    async def get_by_id(self, db: AsyncSession, link_id: str) -> Optional[PresentationLink]:
        try:
            link_uuid = uuid.UUID(str(link_id))
        except (ValueError, AttributeError):
            return None
        result = await db.execute(select(PresentationLink).where(PresentationLink.id == link_uuid))
        return result.scalar_one_or_none()

    async def list_for_file(self, db: AsyncSession, file_id: str) -> List[PresentationLink]:
        try:
            file_uuid = uuid.UUID(str(file_id))
        except (ValueError, AttributeError):
            return []
        result = await db.execute(
            select(PresentationLink)
            .where(PresentationLink.file_id == file_uuid)
            .order_by(PresentationLink.created_at.desc())
        )
        return list(result.scalars().all())

    async def count_created_since(self, db: AsyncSession, created_by: str, since: datetime) -> int:
        from sqlalchemy import func
        try:
            user_uuid = uuid.UUID(str(created_by))
        except (ValueError, AttributeError):
            return 0
        result = await db.execute(
            select(func.count()).select_from(PresentationLink).where(
                and_(
                    PresentationLink.created_by == user_uuid,
                    PresentationLink.created_at >= since,
                )
            )
        )
        return result.scalar() or 0

    async def revoke(self, db: AsyncSession, link_id: str) -> Optional[PresentationLink]:
        now = datetime.utcnow()
        await db.execute(
            update(PresentationLink)
            .where(PresentationLink.id == uuid.UUID(str(link_id)))
            .values(revoked_at=now, updated_at=now)
        )
        await db.flush()
        return await self.get_by_id(db, link_id)

    async def record_access(self, db: AsyncSession, link_id: uuid.UUID) -> None:
        """Atomic increment — access_count = access_count + 1 avoids a
        read-modify-write race under concurrent viewers of the same link."""
        now = datetime.utcnow()
        await db.execute(
            update(PresentationLink)
            .where(PresentationLink.id == link_id)
            .values(access_count=PresentationLink.access_count + 1, last_accessed_at=now)
        )
        await db.flush()


presentation_link_repo = PresentationLinkRepository()
