#!/usr/bin/env python3
"""Reset database - DROP ALL TABLES and recreate them."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.database.connection import db_manager
from app.database.models import Base
from app.core.logging import configure_logging, get_logger
from app.core.config import settings

configure_logging()
logger = get_logger(__name__)


async def reset_database():
    """Reset the database by dropping all tables and recreating them."""
    try:
        logger.warning("⚠️  This will DROP ALL TABLES and DELETE ALL DATA!")

        # Initialize database connection
        await db_manager.initialize()

        # Drop all tables
        logger.info("Dropping all tables...")
        async with db_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

        logger.info("✅ All tables dropped")

        # Recreate all tables
        logger.info("Creating all tables...")
        async with db_manager.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("✅ All tables recreated")

        return True

    except Exception as e:
        logger.error(f"❌ Database reset failed: {e}")
        return False
    finally:
        await db_manager.close()


async def main():
    """Main function."""
    print("🗑️  Database Reset Script")
    print("=" * 50)
    print("⚠️  WARNING: This will DELETE ALL DATA!")
    print(f"Database URL: {settings.database.url}")
    print("=" * 50)

    confirm = input("Type 'yes' to continue: ").strip().lower()
    if confirm != 'yes':
        print("❌ Operation cancelled.")
        return

    success = await reset_database()

    if success:
        print("\n🎉 Database reset completed successfully!")
    else:
        print("\n❌ Database reset failed.")

    print("\n" + "=" * 50)
    print("Reset completed!")


if __name__ == "__main__":
    asyncio.run(main())
