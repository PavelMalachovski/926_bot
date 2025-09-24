#!/usr/bin/env python3
"""Modern database setup script for the FastAPI application."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.database.connection import db_manager
from app.core.logging import configure_logging, get_logger
from app.core.config import settings

configure_logging()
logger = get_logger(__name__)


async def setup_database():
    """Set up the database tables."""
    try:
        logger.info("Setting up database...")

        # Initialize database connection
        await db_manager.initialize()

        logger.info("✅ Database setup completed successfully!")
        return True

    except Exception as e:
        logger.error(f"❌ Database setup failed: {e}")
        return False
    finally:
        await db_manager.close()


async def check_database_health():
    """Check database health."""
    try:
        logger.info("Checking database health...")

        # Initialize database connection
        await db_manager.initialize()

        # Test basic operations
        async for session in db_manager.get_session_async():
            # Test a simple query
            from sqlalchemy import text
            result = await session.execute(text("SELECT 1"))
            result.scalar()
            break

        logger.info("✅ Database health check passed!")
        return True

    except Exception as e:
        logger.error(f"❌ Database health check failed: {e}")
        return False
    finally:
        await db_manager.close()


async def main():
    """Main function."""
    print("🗄️ Modern Database Setup Script")
    print("=" * 50)
    print(f"Database URL: {settings.database.url}")
    print(f"Environment: {settings.environment}")
    print("=" * 50)

    # Set up database
    setup_success = await setup_database()

    if setup_success:
        # Check database health
        health_success = await check_database_health()

        if health_success:
            print("\n🎉 Database is ready for use!")
        else:
            print("\n❌ Database health check failed.")
    else:
        print("\n❌ Database setup failed.")

    print("\n" + "=" * 50)
    print("Setup completed!")


if __name__ == "__main__":
    asyncio.run(main())
