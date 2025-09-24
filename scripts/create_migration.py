#!/usr/bin/env python3
"""Create a new Alembic migration."""

import argparse
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from alembic import command
from alembic.config import Config
from app.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


def create_migration(message: str, autogenerate: bool = True):
    """Create a new migration."""
    try:
        # Configure Alembic
        alembic_cfg = Config("alembic.ini")

        if autogenerate:
            logger.info(f"Creating auto-generated migration: {message}")
            command.revision(alembic_cfg, message=message, autogenerate=True)
        else:
            logger.info(f"Creating empty migration: {message}")
            command.revision(alembic_cfg, message=message, autogenerate=False)

        logger.info("✅ Migration created successfully")

    except Exception as e:
        logger.error(f"❌ Failed to create migration: {e}")
        raise


def main():
    """Main function for command line interface."""
    parser = argparse.ArgumentParser(description="Create a new Alembic migration")
    parser.add_argument(
        "message",
        help="Migration message/description"
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="Create an empty migration (no autogenerate)"
    )

    args = parser.parse_args()

    create_migration(args.message, autogenerate=not args.empty)


if __name__ == "__main__":
    main()
