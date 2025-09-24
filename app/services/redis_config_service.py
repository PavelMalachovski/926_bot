"""Redis configuration and management service."""

import asyncio
from typing import Dict, Any, Optional, List
import structlog

from app.services.cache_service import cache_service
from app.core.config import settings

logger = structlog.get_logger(__name__)


class RedisConfigService:
    """Service for managing Redis configuration and operations."""

    def __init__(self):
        self.cache_service = cache_service

    async def get_redis_info(self) -> Dict[str, Any]:
        """Get comprehensive Redis server information."""
        if not self.cache_service.redis_client:
            return {"error": "Redis client not available"}

        try:
            info = await self.cache_service.redis_client.info()

            # Extract relevant information
            return {
                "server": {
                    "version": info.get("redis_version"),
                    "mode": info.get("redis_mode"),
                    "os": info.get("os"),
                    "arch_bits": info.get("arch_bits"),
                    "uptime_in_seconds": info.get("uptime_in_seconds"),
                    "uptime_in_days": info.get("uptime_in_days")
                },
                "memory": {
                    "used_memory": info.get("used_memory"),
                    "used_memory_human": info.get("used_memory_human"),
                    "used_memory_rss": info.get("used_memory_rss"),
                    "used_memory_peak": info.get("used_memory_peak"),
                    "used_memory_peak_human": info.get("used_memory_peak_human"),
                    "maxmemory": info.get("maxmemory"),
                    "maxmemory_human": info.get("maxmemory_human"),
                    "maxmemory_policy": info.get("maxmemory_policy")
                },
                "clients": {
                    "connected_clients": info.get("connected_clients"),
                    "client_longest_output_list": info.get("client_longest_output_list"),
                    "client_biggest_input_buf": info.get("client_biggest_input_buf"),
                    "blocked_clients": info.get("blocked_clients")
                },
                "stats": {
                    "total_commands_processed": info.get("total_commands_processed"),
                    "instantaneous_ops_per_sec": info.get("instantaneous_ops_per_sec"),
                    "total_net_input_bytes": info.get("total_net_input_bytes"),
                    "total_net_output_bytes": info.get("total_net_output_bytes"),
                    "instantaneous_input_kbps": info.get("instantaneous_input_kbps"),
                    "instantaneous_output_kbps": info.get("instantaneous_output_kbps")
                },
                "keyspace": {
                    "keyspace_hits": info.get("keyspace_hits"),
                    "keyspace_misses": info.get("keyspace_misses"),
                    "expired_keys": info.get("expired_keys"),
                    "evicted_keys": info.get("evicted_keys")
                },
                "persistence": {
                    "loading": info.get("loading"),
                    "rdb_changes_since_last_save": info.get("rdb_changes_since_last_save"),
                    "rdb_last_save_time": info.get("rdb_last_save_time"),
                    "rdb_last_bgsave_status": info.get("rdb_last_bgsave_status"),
                    "aof_enabled": info.get("aof_enabled"),
                    "aof_rewrite_in_progress": info.get("aof_rewrite_in_progress")
                }
            }
        except Exception as e:
            logger.error("Failed to get Redis info", error=str(e))
            return {"error": str(e)}

    async def get_redis_config(self) -> Dict[str, Any]:
        """Get Redis configuration."""
        if not self.cache_service.redis_client:
            return {"error": "Redis client not available"}

        try:
            config = await self.cache_service.redis_client.config_get()

            # Filter relevant configuration parameters
            relevant_config = {}
            for key, value in config.items():
                if any(keyword in key.lower() for keyword in [
                    "maxmemory", "timeout", "tcp", "port", "bind", "save", "dir", "dbfilename"
                ]):
                    relevant_config[key] = value

            return relevant_config
        except Exception as e:
            logger.error("Failed to get Redis config", error=str(e))
            return {"error": str(e)}

    async def get_database_info(self) -> Dict[str, Any]:
        """Get information about Redis databases."""
        if not self.cache_service.redis_client:
            return {"error": "Redis client not available"}

        try:
            db_info = {}

            # Check each database (0-15)
            for db_num in range(16):
                try:
                    # Switch to database
                    await self.cache_service.redis_client.select(db_num)

                    # Get database info
                    info = await self.cache_service.redis_client.info("keyspace")

                    if f"db{db_num}" in info:
                        db_info[f"db{db_num}"] = info[f"db{db_num}"]
                    else:
                        db_info[f"db{db_num}"] = {"keys": 0, "expires": 0, "avg_ttl": 0}

                except Exception as e:
                    logger.warning(f"Failed to get info for db{db_num}", error=str(e))
                    db_info[f"db{db_num}"] = {"error": str(e)}

            # Switch back to default database
            await self.cache_service.redis_client.select(0)

            return db_info
        except Exception as e:
            logger.error("Failed to get database info", error=str(e))
            return {"error": str(e)}

    async def get_slow_log(self, count: int = 10) -> List[Dict[str, Any]]:
        """Get Redis slow log entries."""
        if not self.cache_service.redis_client:
            return []

        try:
            slow_log = await self.cache_service.redis_client.slowlog_get(count)

            formatted_log = []
            for entry in slow_log:
                formatted_log.append({
                    "id": entry[0],
                    "timestamp": entry[1],
                    "duration": entry[2],
                    "command": entry[3],
                    "client": entry[4],
                    "client_name": entry[5]
                })

            return formatted_log
        except Exception as e:
            logger.error("Failed to get slow log", error=str(e))
            return []

    async def get_memory_usage(self, pattern: str = "*") -> Dict[str, Any]:
        """Get memory usage for keys matching pattern."""
        if not self.cache_service.redis_client:
            return {"error": "Redis client not available"}

        try:
            keys = await self.cache_service.redis_client.keys(pattern)

            if not keys:
                return {"pattern": pattern, "keys": 0, "total_memory": 0}

            # Get memory usage for each key
            memory_usage = {}
            total_memory = 0

            for key in keys:
                try:
                    memory = await self.cache_service.redis_client.memory_usage(key)
                    memory_usage[key] = memory
                    total_memory += memory
                except Exception as e:
                    logger.warning(f"Failed to get memory usage for key {key}", error=str(e))
                    memory_usage[key] = 0

            return {
                "pattern": pattern,
                "keys": len(keys),
                "total_memory": total_memory,
                "memory_per_key": memory_usage
            }
        except Exception as e:
            logger.error("Failed to get memory usage", pattern=pattern, error=str(e))
            return {"error": str(e)}

    async def optimize_redis(self) -> Dict[str, Any]:
        """Optimize Redis configuration for better performance."""
        if not self.cache_service.redis_client:
            return {"error": "Redis client not available"}

        try:
            optimizations = []

            # Set memory policy to allkeys-lru
            try:
                await self.cache_service.redis_client.config_set("maxmemory-policy", "allkeys-lru")
                optimizations.append("Set maxmemory-policy to allkeys-lru")
            except Exception as e:
                optimizations.append(f"Failed to set maxmemory-policy: {e}")

            # Enable keyspace notifications
            try:
                await self.cache_service.redis_client.config_set("notify-keyspace-events", "Ex")
                optimizations.append("Enabled keyspace notifications")
            except Exception as e:
                optimizations.append(f"Failed to enable keyspace notifications: {e}")

            # Set timeout for idle clients
            try:
                await self.cache_service.redis_client.config_set("timeout", "300")
                optimizations.append("Set client timeout to 300 seconds")
            except Exception as e:
                optimizations.append(f"Failed to set timeout: {e}")

            # Disable some expensive commands in production
            if settings.environment == "production":
                try:
                    await self.cache_service.redis_client.config_set("rename-command", "FLUSHDB \"\"")
                    await self.cache_service.redis_client.config_set("rename-command", "FLUSHALL \"\"")
                    optimizations.append("Disabled dangerous commands in production")
                except Exception as e:
                    optimizations.append(f"Failed to disable commands: {e}")

            return {
                "status": "completed",
                "optimizations": optimizations
            }
        except Exception as e:
            logger.error("Failed to optimize Redis", error=str(e))
            return {"error": str(e)}

    async def health_check(self) -> Dict[str, Any]:
        """Comprehensive Redis health check."""
        if not self.cache_service.redis_client:
            return {
                "status": "unhealthy",
                "reason": "Redis client not available"
            }

        try:
            # Test basic connectivity
            await self.cache_service.redis_client.ping()

            # Get basic info
            info = await self.cache_service.redis_client.info()

            # Check memory usage
            memory_usage = info.get("used_memory", 0)
            max_memory = info.get("maxmemory", 0)
            memory_percentage = (memory_usage / max_memory * 100) if max_memory > 0 else 0

            # Check if memory usage is high
            memory_warning = memory_percentage > 80 if max_memory > 0 else False

            # Check connected clients
            connected_clients = info.get("connected_clients", 0)
            client_warning = connected_clients > 100

            # Check if Redis is in loading state
            loading = info.get("loading", 0) == 1

            # Determine overall health
            if loading:
                status = "loading"
            elif memory_warning or client_warning:
                status = "degraded"
            else:
                status = "healthy"

            return {
                "status": status,
                "memory_usage_percentage": round(memory_percentage, 2),
                "connected_clients": connected_clients,
                "loading": loading,
                "warnings": {
                    "high_memory_usage": memory_warning,
                    "high_client_count": client_warning
                },
                "uptime_seconds": info.get("uptime_in_seconds", 0)
            }
        except Exception as e:
            logger.error("Redis health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "reason": str(e)
            }


# Global Redis config service instance
redis_config_service = RedisConfigService()
