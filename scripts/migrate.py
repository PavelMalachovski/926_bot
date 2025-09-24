#!/usr/bin/env python3
"""Database migration script for the modern FastAPI application."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from alembic import command
from alembic.config import Config
from app.database.connection import db_manager
from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


async def run_migrations():
    """Run database migrations."""
    try:
        # Initialize database connection
        await db_manager.initialize()

        # Configure Alembic
        alembic_cfg = Config("alembic.ini")

        # Run migrations
        logger.info("Running database migrations...")
        command.upgrade(alembic_cfg, "head")

        logger.info("Database migrations completed successfully")

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(run_migrations())
