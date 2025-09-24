"""Main FastAPI application entry point."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import time
import structlog

from app.core.config import settings
from app.core.logging import configure_logging
from app.core.exceptions import ForexBotException, ValidationError
from app.core.security import add_security_headers, log_requests, get_cors_config
from app.database.connection import db_manager
from app.services.cache_service import cache_service
from app.api.v1.router import api_router

# Configure structured logging
configure_logging()
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Forex Bot application", version=settings.app_version)

    try:
        # Initialize database
        await db_manager.initialize()
        logger.info("Database initialized successfully")

        # Initialize cache service
        await cache_service.initialize()
        logger.info("Cache service initialized successfully")

        # Initialize Telegram bot
        # await telegram_service.initialize()

        logger.info("Application startup completed")
        yield

    except Exception as e:
        logger.error("Failed to start application", error=str(e), exc_info=True)
        raise

    finally:
        # Shutdown
        logger.info("Shutting down application")
        await cache_service.close()
        await db_manager.close()
        logger.info("Application shutdown completed")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""

    # Create FastAPI application
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=settings.app_description,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    # Add rate limiting
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Add middleware
    cors_config = get_cors_config()
    app.add_middleware(
        CORSMiddleware,
        **cors_config
    )

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*"] if settings.debug else ["yourdomain.com", "*.yourdomain.com", "testserver", "localhost", "127.0.0.1"]
    )

    # Add security and logging middleware
    app.middleware("http")(add_security_headers)
    app.middleware("http")(log_requests)

    # Request timing middleware
    @app.middleware("http")
    async def add_process_time_header(request: Request, call_next):
        """Add processing time to response headers."""
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        return response

    # Global exception handler
    @app.exception_handler(ForexBotException)
    async def forex_bot_exception_handler(request: Request, exc: ForexBotException):
        """Handle custom forex bot exceptions."""
        logger.error(
            "ForexBotException",
            error_code=exc.error_code,
            message=exc.message,
            details=exc.details,
            path=request.url.path,
            method=request.method,
        )

        # Handle ValidationError with 400 status code
        if isinstance(exc, ValidationError):
            return JSONResponse(
                status_code=400,
                content={"detail": exc.message}
            )

        return JSONResponse(
            status_code=400,
            content={
                "error": exc.message,
                "error_code": exc.error_code,
                "details": exc.details,
            }
        )


    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """Handle HTTP exceptions."""
        logger.warning(
            "HTTPException",
            status_code=exc.status_code,
            detail=exc.detail,
            path=request.url.path,
            method=request.method,
        )

        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail}
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        """Handle general exceptions."""
        logger.error(
            "Unhandled exception",
            error=str(exc),
            path=request.url.path,
            method=request.method,
            exception_type=type(exc).__name__,
            exc_info=True
        )

        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"}
        )

    # Health check endpoint
    @app.get("/health", tags=["health"])
    async def health_check():
        """Health check endpoint."""
        return {
            "status": "healthy",
            "version": settings.app_version,
            "environment": settings.environment,
            "timestamp": time.time(),
        }

    # Include API routes
    app.include_router(api_router, prefix="/api/v1")

    # Root endpoint
    @app.get("/", tags=["root"])
    async def root():
        """Root endpoint."""
        return {
            "message": f"Welcome to {settings.app_name}",
            "version": settings.app_version,
            "docs": "/docs" if settings.debug else "Documentation not available in production",
            "health": "/health",
        }

    return app


# Create the application instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.logging.level.lower(),
    )
