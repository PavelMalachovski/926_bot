"""User service implementation."""

from typing import Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, update

from .base import BaseService
from app.database.models import UserModel
from app.models.user import UserCreate, UserUpdate, UserPreferences
from app.core.exceptions import DatabaseError, ValidationError
import structlog

logger = structlog.get_logger(__name__)


class UserService(BaseService[UserModel]):
    """User service with business logic."""

    def __init__(self):
        super().__init__(UserModel)

    async def create_user(self, db: AsyncSession, user_data: UserCreate) -> UserModel:
        """Create a new user."""
        try:
            # Check if user already exists
            existing_user = await self.get_by_telegram_id(db, user_data.telegram_id)
            if existing_user:
                raise ValidationError(f"User with Telegram ID {user_data.telegram_id} already exists")

            # Create user data
            user_dict = user_data.model_dump()
            preferences = user_dict.pop("preferences", {})

            # Merge preferences into user data
            user_dict.update(preferences)

            return await self.create(db, **user_dict)
        except ValidationError:
            raise
        except Exception as e:
            logger.error("Failed to create user", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to create user: {e}")

    async def get_by_telegram_id(self, db: AsyncSession, telegram_id: int) -> Optional[UserModel]:
        """Get user by Telegram ID."""
        try:
            result = await db.execute(
                select(UserModel).where(UserModel.telegram_id == telegram_id)
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error("Failed to get user by Telegram ID", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get user by Telegram ID: {e}")

    async def update_user(
        self,
        db: AsyncSession,
        telegram_id: int,
        user_data: UserUpdate
    ) -> Optional[UserModel]:
        """Update user by Telegram ID."""
        try:
            user = await self.get_by_telegram_id(db, telegram_id)
            if not user:
                raise ValidationError("User not found")

            update_data = user_data.model_dump(exclude_unset=True)
            if not update_data:
                return user

            # Handle preferences separately
            preferences = update_data.pop("preferences", None)
            if preferences:
                # Update preferences fields
                for key, value in preferences.items():
                    if hasattr(user, key):
                        setattr(user, key, value)

            # Update other fields
            for key, value in update_data.items():
                if hasattr(user, key):
                    setattr(user, key, value)

            await db.commit()
            await db.refresh(user)
            return user
        except ValidationError:
            # Re-raise ValidationError without wrapping
            raise
        except Exception as e:
            await db.rollback()
            logger.error("Failed to update user", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update user: {e}")

    async def update_preferences(
        self,
        db: AsyncSession,
        telegram_id: int,
        preferences: UserPreferences
    ) -> Optional[UserModel]:
        """Update user preferences."""
        try:
            user = await self.get_by_telegram_id(db, telegram_id)
            if not user:
                return None

            # Update preferences fields
            preferences_dict = preferences.model_dump()
            for key, value in preferences_dict.items():
                if hasattr(user, key):
                    setattr(user, key, value)

            await db.commit()
            await db.refresh(user)
            return user
        except Exception as e:
            await db.rollback()
            logger.error("Failed to update user preferences", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update user preferences: {e}")

    async def get_active_users(self, db: AsyncSession) -> List[UserModel]:
        """Get all active users."""
        try:
            result = await db.execute(
                select(UserModel).where(UserModel.is_active == True)
            )
            return (await result.scalars()).all()
        except Exception as e:
            logger.error("Failed to get active users", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get active users: {e}")

    async def get_users_by_currency(self, db: AsyncSession, currency: str) -> List[UserModel]:
        """Get users who have a specific currency in their preferences."""
        try:
            result = await db.execute(
                select(UserModel).where(
                    and_(
                        UserModel.is_active == True,
                        UserModel.preferred_currencies.contains([currency])
                    )
                )
            )
            return (await result.scalars()).all()
        except Exception as e:
            logger.error("Failed to get users by currency", currency=currency, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get users by currency: {e}")

    async def get_users_by_impact_level(self, db: AsyncSession, impact_level: str) -> List[UserModel]:
        """Get users who have a specific impact level in their preferences."""
        try:
            result = await db.execute(
                select(UserModel).where(
                    and_(
                        UserModel.is_active == True,
                        UserModel.impact_levels.contains([impact_level])
                    )
                )
            )
            return (await result.scalars()).all()
        except Exception as e:
            logger.error("Failed to get users by impact level", impact_level=impact_level, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get users by impact level: {e}")

    async def update_last_active(self, db: AsyncSession, telegram_id: int) -> bool:
        """Update user's last active timestamp."""
        try:
            from datetime import datetime
            await db.execute(
                update(UserModel)
                .where(UserModel.telegram_id == telegram_id)
                .values(last_active=datetime.utcnow())
            )
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            logger.error("Failed to update last active", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update last active: {e}")

    async def deactivate_user(self, db: AsyncSession, telegram_id: int) -> bool:
        """Deactivate a user."""
        try:
            await db.execute(
                update(UserModel)
                .where(UserModel.telegram_id == telegram_id)
                .values(is_active=False)
            )
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            logger.error("Failed to deactivate user", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to deactivate user: {e}")

    # Additional methods expected by tests
    async def get_user_by_telegram_id(self, db: AsyncSession, telegram_id: int) -> Optional[UserModel]:
        """Get user by Telegram ID (alias for get_by_telegram_id)."""
        return await self.get_by_telegram_id(db, telegram_id)

    async def get_all_users_with_pagination(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100
    ) -> List[UserModel]:
        """Get all users with pagination."""
        try:
            result = await db.execute(
                select(UserModel)
                .offset(skip)
                .limit(limit)
                .order_by(UserModel.created_at.desc())
            )
            return (await result.scalars()).all()
        except Exception as e:
            logger.error("Failed to get users with pagination", skip=skip, limit=limit, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get users with pagination: {e}")

    async def count_users(self, db: AsyncSession) -> int:
        """Count total users."""
        try:
            result = await db.execute(select(func.count(UserModel.id)))
            return result.scalar() or 0
        except Exception as e:
            logger.error("Failed to count users", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to count users: {e}")

    async def user_exists(self, db: AsyncSession, telegram_id: int) -> bool:
        """Check if user exists."""
        try:
            result = await db.execute(
                select(UserModel.id).where(UserModel.telegram_id == telegram_id)
            )
            return result.scalar_one_or_none() is not None
        except Exception as e:
            logger.error("Failed to check user existence", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to check user existence: {e}")

    async def update_user_preferences(self, db: AsyncSession, telegram_id: int, preferences: UserPreferences) -> UserModel:
        """Update user preferences."""
        try:
            user = await self.get_by_telegram_id(db, telegram_id)
            if not user:
                raise ValidationError("User not found")

            # Update preferences
            preferences_dict = preferences.model_dump()
            for key, value in preferences_dict.items():
                setattr(user, key, value)

            await db.commit()
            await db.refresh(user)
            return user
        except ValidationError:
            raise
        except Exception as e:
            await db.rollback()
            logger.error("Failed to update user preferences", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update user preferences: {e}")

    async def delete_user(self, db: AsyncSession, telegram_id: int) -> bool:
        """Delete user by Telegram ID."""
        try:
            user = await self.get_by_telegram_id(db, telegram_id)
            if not user:
                return False

            await db.delete(user)
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            logger.error("Failed to delete user", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to delete user: {e}")

    async def update_user_activity(self, db: AsyncSession, telegram_id: int) -> bool:
        """Update user's last active timestamp."""
        try:
            user = await self.get_by_telegram_id(db, telegram_id)
            if not user:
                return False

            user.last_active = datetime.utcnow()
            await db.commit()
            await db.refresh(user)
            return True
        except Exception as e:
            await db.rollback()
            logger.error("Failed to update user activity", telegram_id=telegram_id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update user activity: {e}")
