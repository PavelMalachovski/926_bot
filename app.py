"""Main FastAPI application."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
import time

from .core.config import settings
from .core.logging import configure_logging, get_logger
from .core.exceptions import ForexBotException
from .database.connection import db_manager
from .api.v1.router import api_router


# Configure logging
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Forex Bot application...")

    try:
        # Initialize database
        await db_manager.initialize()
        logger.info("Database initialized successfully")

        # Initialize other services here
        # await redis_manager.initialize()
        # await telegram_service.initialize()

        logger.info("Application startup completed")
        yield

    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        raise

    finally:
        # Shutdown
        logger.info("Shutting down application...")
        await db_manager.close()
        logger.info("Application shutdown completed")


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=settings.app_description,
    debug=settings.debug,
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# Add middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else ["https://yourdomain.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"] if settings.debug else ["yourdomain.com", "*.yourdomain.com"]
)


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
    logger.error(f"ForexBotException: {exc.message}", extra={
        "error_code": exc.error_code,
        "details": exc.details,
        "path": request.url.path,
        "method": request.method,
    })

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
    logger.warning(f"HTTPException: {exc.detail}", extra={
        "status_code": exc.status_code,
        "path": request.url.path,
        "method": request.method,
    })

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail}
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions."""
    logger.error(f"Unhandled exception: {exc}", extra={
        "path": request.url.path,
        "method": request.method,
        "exception_type": type(exc).__name__,
    }, exc_info=True)

    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )


# Health check endpoint
@app.get("/health")
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
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": f"Welcome to {settings.app_name}",
        "version": settings.app_version,
        "docs": "/docs" if settings.debug else "Documentation not available in production",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.logging.level.lower(),
    )
