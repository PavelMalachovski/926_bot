"""Database connection and session management."""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
import structlog

from app.core.config import settings
from app.database.models import Base

logger = structlog.get_logger(__name__)


class DatabaseManager:
    """Database connection manager."""

    def __init__(self):
        self.engine = None
        self.session_factory = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize database connection."""
        if self._initialized:
            return

        try:
            # Create async engine
            engine_kwargs = {
                "echo": settings.database.echo,
            }

            # Add SQLite-specific configuration
            if "sqlite" in settings.database.url:
                engine_kwargs.update({
                    "poolclass": StaticPool,
                    "connect_args": {"check_same_thread": False},
                })
            else:
                # PostgreSQL/other databases
                engine_kwargs.update({
                    "pool_size": settings.database.pool_size,
                    "max_overflow": settings.database.max_overflow,
                })

            self.engine = create_async_engine(settings.database.url, **engine_kwargs)

            # Create session factory
            self.session_factory = async_sessionmaker(
                self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            # Create all tables
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            self._initialized = True
            logger.info("Database initialized successfully")

        except Exception as e:
            logger.error("Failed to initialize database", error=str(e), exc_info=True)
            raise

    async def close(self) -> None:
        """Close database connection."""
        if self.engine:
            await self.engine.dispose()
            logger.info("Database connection closed")

    def get_session(self) -> AsyncSession:
        """Get database session."""
        if not self._initialized:
            raise RuntimeError("Database not initialized")
        return self.session_factory()

    async def get_session_async(self) -> AsyncGenerator[AsyncSession, None]:
        """Get async database session."""
        session = self.session_factory()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Global database manager instance
db_manager = DatabaseManager()


async def get_database() -> AsyncGenerator[AsyncSession, None]:
    """Dependency to get database session."""
    async for session in db_manager.get_session_async():
        yield session
