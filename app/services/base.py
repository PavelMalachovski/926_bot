"""Base service class with common CRUD operations."""

from typing import TypeVar, Generic, Type, Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.orm import DeclarativeBase
import structlog

from app.core.exceptions import DatabaseError, ValidationError

logger = structlog.get_logger(__name__)

ModelType = TypeVar("ModelType", bound=DeclarativeBase)


class BaseService(Generic[ModelType]):
    """Base service class with common CRUD operations."""

    def __init__(self, model: Type[ModelType]):
        self.model = model
        self.logger = logger.bind(service=self.model.__name__)

    async def create(self, db: AsyncSession, **kwargs) -> ModelType:
        """Create a new record."""
        try:
            instance = self.model(**kwargs)
            db.add(instance)
            await db.commit()
            await db.refresh(instance)
            return instance
        except Exception as e:
            await db.rollback()
            self.logger.error("Failed to create record", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to create {self.model.__name__}: {e}")

    async def get(self, db: AsyncSession, id: int) -> Optional[ModelType]:
        """Get a record by ID."""
        try:
            result = await db.execute(select(self.model).where(self.model.id == id))
            return result.scalar_one_or_none()
        except Exception as e:
            self.logger.error("Failed to get record", id=id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get {self.model.__name__}: {e}")

    async def get_all(
        self,
        db: AsyncSession,
        skip: int = 0,
        limit: int = 100,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[ModelType]:
        """Get all records with optional filtering."""
        try:
            query = select(self.model)

            if filters:
                for key, value in filters.items():
                    if hasattr(self.model, key):
                        query = query.where(getattr(self.model, key) == value)

            query = query.offset(skip).limit(limit)
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            self.logger.error("Failed to get records", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to get {self.model.__name__} records: {e}")

    async def update(self, db: AsyncSession, id: int, **kwargs) -> Optional[ModelType]:
        """Update a record by ID."""
        try:
            # Check if record exists first
            existing_record = await self.get(db, id)
            if not existing_record:
                raise ValidationError(f"{self.model.__name__} not found")

            # Remove None values
            update_data = {k: v for k, v in kwargs.items() if v is not None}

            if not update_data:
                return existing_record

            await db.execute(
                update(self.model)
                .where(self.model.id == id)
                .values(**update_data)
            )
            await db.commit()
            return await self.get(db, id)
        except ValidationError:
            # Re-raise ValidationError directly to preserve status code
            raise
        except Exception as e:
            await db.rollback()
            self.logger.error("Failed to update record", id=id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to update {self.model.__name__}: {e}")

    async def delete(self, db: AsyncSession, id: int) -> bool:
        """Delete a record by ID."""
        try:
            await db.execute(delete(self.model).where(self.model.id == id))
            await db.commit()
            return True
        except Exception as e:
            await db.rollback()
            self.logger.error("Failed to delete record", id=id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to delete {self.model.__name__}: {e}")

    async def count(
        self,
        db: AsyncSession,
        filters: Optional[Dict[str, Any]] = None
    ) -> int:
        """Count records with optional filtering."""
        try:
            query = select(self.model)

            if filters:
                for key, value in filters.items():
                    if hasattr(self.model, key):
                        query = query.where(getattr(self.model, key) == value)

            result = await db.execute(query)
            return len(result.scalars().all())
        except Exception as e:
            self.logger.error("Failed to count records", error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to count {self.model.__name__} records: {e}")

    async def exists(self, db: AsyncSession, id: int) -> bool:
        """Check if a record exists."""
        try:
            result = await db.execute(select(self.model).where(self.model.id == id))
            return result.scalar_one_or_none() is not None
        except Exception as e:
            self.logger.error("Failed to check existence", id=id, error=str(e), exc_info=True)
            raise DatabaseError(f"Failed to check {self.model.__name__} existence: {e}")
