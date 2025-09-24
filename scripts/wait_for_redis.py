#!/usr/bin/env python3
"""Wait for Redis to be ready before starting the application."""

import asyncio
import sys
import time
import os
from typing import Optional
import redis.asyncio as redis
import structlog

logger = structlog.get_logger(__name__)


class RedisWaiter:
    """Wait for Redis to be available."""

    def __init__(
        self,
        redis_host: str = "redis",
        redis_port: int = 6379,
        redis_password: Optional[str] = None,
        redis_db: int = 0,
        max_retries: int = 30,
        retry_delay: int = 2
    ):
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_password = redis_password
        self.redis_db = redis_db
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.redis_client = None

    def _build_redis_url(self) -> str:
        """Build Redis URL from components."""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        else:
            return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    async def _test_redis_connection(self) -> bool:
        """Test Redis connection."""
        try:
            redis_url = self._build_redis_url()
            self.redis_client = redis.from_url(redis_url)

            # Test ping
            await asyncio.wait_for(self.redis_client.ping(), timeout=5.0)

            # Test basic operations
            await self.redis_client.set("health_check", "ok", ex=10)
            result = await self.redis_client.get("health_check")

            if result == "ok":
                await self.redis_client.delete("health_check")
                return True

            return False

        except Exception as e:
            logger.debug("Redis connection test failed", error=str(e))
            return False
        finally:
            if self.redis_client:
                await self.redis_client.close()
                self.redis_client = None

    async def wait_for_redis(self) -> bool:
        """Wait for Redis to be ready."""
        logger.info(
            "Waiting for Redis to be ready",
            host=self.redis_host,
            port=self.redis_port,
            max_retries=self.max_retries
        )

        for attempt in range(self.max_retries):
            try:
                logger.debug(f"Redis connection attempt {attempt + 1}/{self.max_retries}")

                if await self._test_redis_connection():
                    logger.info("Redis is ready!", attempt=attempt + 1)
                    return True

                logger.debug(f"Redis not ready, waiting {self.retry_delay}s before retry")
                await asyncio.sleep(self.retry_delay)

            except Exception as e:
                logger.warning(f"Redis connection attempt {attempt + 1} failed", error=str(e))
                await asyncio.sleep(self.retry_delay)

        logger.error("Redis failed to become ready within timeout", max_retries=self.max_retries)
        return False

    async def optimize_redis(self) -> bool:
        """Optimize Redis configuration for the application."""
        try:
            redis_url = self._build_redis_url()
            self.redis_client = redis.from_url(redis_url)

            logger.info("Optimizing Redis configuration...")

            # Set memory policy to allkeys-lru for better cache management
            try:
                await self.redis_client.config_set("maxmemory-policy", "allkeys-lru")
                logger.info("Set maxmemory-policy to allkeys-lru")
            except Exception as e:
                logger.warning("Failed to set maxmemory-policy", error=str(e))

            # Enable keyspace notifications for cache invalidation
            try:
                await self.redis_client.config_set("notify-keyspace-events", "Ex")
                logger.info("Enabled keyspace notifications")
            except Exception as e:
                logger.warning("Failed to enable keyspace notifications", error=str(e))

            # Set timeout for idle clients
            try:
                await self.redis_client.config_set("timeout", "300")
                logger.info("Set client timeout to 300 seconds")
            except Exception as e:
                logger.warning("Failed to set timeout", error=str(e))

            # Disable dangerous commands in production
            if os.getenv("ENVIRONMENT") == "production":
                try:
                    await self.redis_client.config_set("rename-command", "FLUSHDB \"\"")
                    await self.redis_client.config_set("rename-command", "FLUSHALL \"\"")
                    logger.info("Disabled dangerous commands in production")
                except Exception as e:
                    logger.warning("Failed to disable dangerous commands", error=str(e))

            await self.redis_client.close()
            logger.info("Redis optimization completed")
            return True

        except Exception as e:
            logger.error("Failed to optimize Redis", error=str(e))
            return False


async def main():
    """Main function."""
    # Get Redis configuration from environment variables
    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    redis_password = os.getenv("REDIS_PASSWORD")
    redis_db = int(os.getenv("REDIS_DB", "0"))

    # Get wait configuration
    max_retries = int(os.getenv("REDIS_MAX_RETRIES", "30"))
    retry_delay = int(os.getenv("REDIS_RETRY_DELAY", "2"))

    # Create Redis waiter
    waiter = RedisWaiter(
        redis_host=redis_host,
        redis_port=redis_port,
        redis_password=redis_password,
        redis_db=redis_db,
        max_retries=max_retries,
        retry_delay=retry_delay
    )

    # Wait for Redis
    if not await waiter.wait_for_redis():
        logger.error("Redis is not available, exiting")
        sys.exit(1)

    # Optimize Redis configuration
    await waiter.optimize_redis()

    logger.info("Redis is ready and optimized, starting application...")


if __name__ == "__main__":
    asyncio.run(main())
