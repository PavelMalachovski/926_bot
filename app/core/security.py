"""Security utilities and middleware."""

import time
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT token security
security = HTTPBearer()

# Rate limiter
limiter = Limiter(key_func=get_remote_address)


class SecurityService:
    """Security service for authentication and authorization."""

    def __init__(self):
        self.secret_key = settings.security.secret_key
        self.algorithm = settings.security.algorithm
        self.access_token_expire_minutes = settings.security.access_token_expire_minutes
        self.refresh_token_expire_days = settings.security.refresh_token_expire_days

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """Verify a password against its hash."""
        return pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password: str) -> str:
        """Hash a password."""
        return pwd_context.hash(password)

    def create_access_token(self, data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
        """Create JWT access token."""
        to_encode = data.copy()

        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=self.access_token_expire_minutes)

        to_encode.update({"exp": expire, "type": "access"})
        encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
        return encoded_jwt

    def create_refresh_token(self, data: Dict[str, Any]) -> str:
        """Create JWT refresh token."""
        to_encode = data.copy()
        expire = datetime.utcnow() + timedelta(days=self.refresh_token_expire_days)
        to_encode.update({"exp": expire, "type": "refresh"})
        encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
        return encoded_jwt

    def verify_token(self, token: str, token_type: str = "access") -> Dict[str, Any]:
        """Verify JWT token."""
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])

            # Check token type
            if payload.get("type") != token_type:
                raise HTTPException(status_code=401, detail="Invalid token type")

            # Check expiration
            exp = payload.get("exp")
            if exp and datetime.utcnow() > datetime.fromtimestamp(exp):
                raise HTTPException(status_code=401, detail="Token expired")

            return payload
        except JWTError as e:
            logger.error("JWT verification failed", error=str(e))
            raise HTTPException(status_code=401, detail="Invalid token")


# Global security service instance
security_service = SecurityService()


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict[str, Any]:
    """
    Get current user from JWT token.

    Args:
        credentials: HTTP Bearer credentials

    Returns:
        User data from token

    Raises:
        HTTPException: If token is invalid
    """
    token = credentials.credentials
    payload = security_service.verify_token(token)

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    return {
        "id": user_id,
        "telegram_id": payload.get("telegram_id"),
        "username": payload.get("username"),
        "is_active": payload.get("is_active", True)
    }


async def get_current_active_user(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Get current active user.

    Args:
        current_user: Current user from token

    Returns:
        Active user data

    Raises:
        HTTPException: If user is inactive
    """
    if not current_user.get("is_active", True):
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


def require_api_key(request: Request) -> str:
    """
    Require API key for internal endpoints.

    Args:
        request: FastAPI request object

    Returns:
        API key

    Raises:
        HTTPException: If API key is missing or invalid
    """
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    if api_key != settings.security.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return api_key


# Rate limiting decorators
def rate_limit(calls: int, period: int = 60):
    """
    Rate limiting decorator.

    Args:
        calls: Number of calls allowed
        period: Time period in seconds

    Returns:
        Decorated function
    """
    return limiter.limit(f"{calls}/{period}second")


def rate_limit_per_minute(calls: int):
    """Rate limit per minute."""
    return rate_limit(calls, 60)


def rate_limit_per_hour(calls: int):
    """Rate limit per hour."""
    return rate_limit(calls, 3600)


def rate_limit_per_day(calls: int):
    """Rate limit per day."""
    return rate_limit(calls, 86400)


# Security headers middleware
async def add_security_headers(request: Request, call_next):
    """
    Add security headers to responses.

    Args:
        request: FastAPI request
        call_next: Next middleware

    Returns:
        Response with security headers
    """
    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'"

    # Remove server header
    if "Server" in response.headers:
        del response.headers["Server"]

    return response


# Request logging middleware
async def log_requests(request: Request, call_next):
    """
    Log all requests for security monitoring.

    Args:
        request: FastAPI request
        call_next: Next middleware

    Returns:
        Response
    """
    start_time = time.time()

    # Log request
    logger.info(
        "Request started",
        method=request.method,
        url=str(request.url),
        client_ip=get_remote_address(request),
        user_agent=request.headers.get("User-Agent"),
        content_length=request.headers.get("Content-Length", 0)
    )

    # Process request
    response = await call_next(request)

    # Log response
    process_time = time.time() - start_time
    logger.info(
        "Request completed",
        method=request.method,
        url=str(request.url),
        status_code=response.status_code,
        process_time=process_time,
        client_ip=get_remote_address(request)
    )

    return response


# IP whitelist middleware
class IPWhitelistMiddleware:
    """Middleware for IP whitelisting."""

    def __init__(self, whitelist: list):
        self.whitelist = whitelist

    async def __call__(self, request: Request, call_next):
        client_ip = get_remote_address(request)

        if self.whitelist and client_ip not in self.whitelist:
            logger.warning("Blocked request from non-whitelisted IP", ip=client_ip)
            raise HTTPException(status_code=403, detail="Access denied")

        return await call_next(request)


# CORS configuration
def get_cors_config() -> Dict[str, Any]:
    """Get CORS configuration."""
    return {
        "allow_origins": settings.cors.origins,
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": [
            "Accept",
            "Accept-Language",
            "Content-Language",
            "Content-Type",
            "Authorization",
            "X-API-Key",
            "X-Requested-With"
        ],
        "expose_headers": ["X-Total-Count", "X-Page-Count"],
        "max_age": 3600
    }
