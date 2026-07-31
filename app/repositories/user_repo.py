"""User repository."""
import uuid
from typing import Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User


class UserRepository:

    async def get_or_create(self, db: AsyncSession, external_id: str, email: str, name: str = None) -> User:
        result = await db.execute(
            select(User).where(User.external_id == external_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                id=uuid.uuid4(),
                external_id=external_id,
                email=email,
                name=name,
            )
            db.add(user)
            await db.flush()
            await db.refresh(user)
        return user

    async def get_by_id(self, db: AsyncSession, user_id: str) -> Optional[User]:
        result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
        return result.scalar_one_or_none()


user_repo = UserRepository()
