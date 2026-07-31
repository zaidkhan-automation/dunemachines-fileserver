"""Project repository."""
import uuid
from typing import Optional, List, Tuple, Dict, Any
from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.project import Project


class ProjectRepository:

    async def create(self, db: AsyncSession, data: Dict[str, Any]) -> Project:
        project = Project(
            id=uuid.uuid4(),
            organization_id=uuid.UUID(data["organization_id"]),
            created_by=uuid.UUID(data["created_by"]),
            name=data["name"],
            description=data.get("description"),
            is_public=data.get("is_public", False),
        )
        db.add(project)
        await db.flush()
        await db.refresh(project)
        return project

    async def get_by_id(self, db: AsyncSession, project_id: str, org_id: str) -> Optional[Project]:
        result = await db.execute(
            select(Project).where(
                and_(
                    Project.id == uuid.UUID(project_id),
                    Project.organization_id == uuid.UUID(org_id),
                    Project.is_active == True,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self, db: AsyncSession, org_id: str, limit: int = 50, offset: int = 0
    ) -> Tuple[List[Project], int]:
        filters = [
            Project.organization_id == uuid.UUID(org_id),
            Project.is_active == True,
        ]
        count_result = await db.execute(
            select(func.count()).select_from(Project).where(and_(*filters))
        )
        total = count_result.scalar()
        result = await db.execute(
            select(Project).where(and_(*filters))
            .order_by(Project.created_at.desc())
            .limit(limit).offset(offset)
        )
        return list(result.scalars().all()), total

    async def delete(self, db: AsyncSession, project_id: str, org_id: str) -> bool:
        result = await db.execute(
            update(Project)
            .where(and_(Project.id == uuid.UUID(project_id), Project.organization_id == uuid.UUID(org_id)))
            .values(is_active=False)
        )
        return result.rowcount > 0


project_repo = ProjectRepository()
