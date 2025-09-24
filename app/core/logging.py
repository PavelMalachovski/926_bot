"""Structured logging configuration."""

import logging
import sys
from typing import Optional
import structlog
from structlog.stdlib import LoggerFactory

from app.core.config import settings


def configure_logging(
    log_level: Optional[str] = None,
    log_format: Optional[str] = None,
    log_file: Optional[str] = None
) -> None:
    """Configure structured logging for the application."""

    # Use settings if not provided
    log_level = log_level or settings.logging.level
    log_format = log_format or settings.logging.format
    log_file = log_file or settings.logging.file_path

    # Configure standard library logging
    if log_file:
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format=log_format,
            filename=log_file,
            filemode="a",
        )
    else:
        logging.basicConfig(
            level=getattr(logging, log_level.upper()),
            format=log_format,
            stream=sys.stdout,
        )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if settings.environment == "production" else structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        logger_factory=LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)
