#!/usr/bin/env python3
"""Redis monitoring script for Forex Bot application."""

import asyncio
import json
import time
from datetime import datetime
from typing import Dict, Any
import argparse

import redis.asyncio as redis
from app.services.redis_config_service import redis_config_service


class RedisMonitor:
    """Redis monitoring and health check tool."""

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self.redis_client = None

    async def connect(self):
        """Connect to Redis."""
        try:
            self.redis_client = redis.from_url(self.redis_url)
            await self.redis_client.ping()
            print(f"✅ Connected to Redis at {self.redis_url}")
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Redis: {e}")
            return False

    async def disconnect(self):
        """Disconnect from Redis."""
        if self.redis_client:
            await self.redis_client.close()

    async def get_basic_info(self) -> Dict[str, Any]:
        """Get basic Redis information."""
        try:
            info = await self.redis_client.info()
            return {
                "version": info.get("redis_version"),
                "mode": info.get("redis_mode"),
                "uptime_seconds": info.get("uptime_in_seconds"),
                "uptime_days": info.get("uptime_in_days"),
                "connected_clients": info.get("connected_clients"),
                "used_memory": info.get("used_memory_human"),
                "max_memory": info.get("maxmemory_human"),
                "memory_policy": info.get("maxmemory_policy"),
                "total_commands": info.get("total_commands_processed"),
                "ops_per_sec": info.get("instantaneous_ops_per_sec"),
                "keyspace_hits": info.get("keyspace_hits"),
                "keyspace_misses": info.get("keyspace_misses")
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_memory_usage(self) -> Dict[str, Any]:
        """Get detailed memory usage information."""
        try:
            info = await self.redis_client.info("memory")
            return {
                "used_memory": info.get("used_memory"),
                "used_memory_human": info.get("used_memory_human"),
                "used_memory_rss": info.get("used_memory_rss"),
                "used_memory_peak": info.get("used_memory_peak"),
                "used_memory_peak_human": info.get("used_memory_peak_human"),
                "used_memory_overhead": info.get("used_memory_overhead"),
                "used_memory_startup": info.get("used_memory_startup"),
                "used_memory_dataset": info.get("used_memory_dataset"),
                "used_memory_dataset_percentage": info.get("used_memory_dataset_percentage"),
                "maxmemory": info.get("maxmemory"),
                "maxmemory_human": info.get("maxmemory_human"),
                "maxmemory_policy": info.get("maxmemory_policy"),
                "mem_fragmentation_ratio": info.get("mem_fragmentation_ratio"),
                "mem_fragmentation_bytes": info.get("mem_fragmentation_bytes")
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_keyspace_info(self) -> Dict[str, Any]:
        """Get keyspace information."""
        try:
            info = await self.redis_client.info("keyspace")
            return info
        except Exception as e:
            return {"error": str(e)}

    async def get_slow_log(self, count: int = 10) -> list:
        """Get slow log entries."""
        try:
            slow_log = await self.redis_client.slowlog_get(count)
            formatted_log = []
            for entry in slow_log:
                formatted_log.append({
                    "id": entry[0],
                    "timestamp": datetime.fromtimestamp(entry[1]).isoformat(),
                    "duration": entry[2],
                    "command": entry[3],
                    "client": entry[4],
                    "client_name": entry[5]
                })
            return formatted_log
        except Exception as e:
            return [{"error": str(e)}]

    async def get_client_list(self) -> list:
        """Get connected clients list."""
        try:
            clients = await self.redis_client.client_list()
            return clients
        except Exception as e:
            return [{"error": str(e)}]

    async def monitor_commands(self, duration: int = 10):
        """Monitor Redis commands in real-time."""
        print(f"🔍 Monitoring Redis commands for {duration} seconds...")
        print("Press Ctrl+C to stop")

        try:
            pubsub = self.redis_client.pubsub()
            await pubsub.psubscribe("__keyspace@*__:*")

            start_time = time.time()
            command_count = 0

            async for message in pubsub.listen():
                if message['type'] == 'pmessage':
                    command_count += 1
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message['data']}")

                if time.time() - start_time >= duration:
                    break

            await pubsub.close()
            print(f"\n📊 Monitored {command_count} commands in {duration} seconds")

        except KeyboardInterrupt:
            print("\n⏹️ Monitoring stopped by user")
        except Exception as e:
            print(f"❌ Monitoring error: {e}")

    async def health_check(self) -> Dict[str, Any]:
        """Perform comprehensive health check."""
        print("🏥 Performing Redis health check...")

        health_status = {
            "timestamp": datetime.now().isoformat(),
            "overall_status": "unknown",
            "checks": {}
        }

        # Check connectivity
        try:
            await self.redis_client.ping()
            health_status["checks"]["connectivity"] = {"status": "healthy", "message": "Ping successful"}
        except Exception as e:
            health_status["checks"]["connectivity"] = {"status": "unhealthy", "message": str(e)}
            health_status["overall_status"] = "unhealthy"
            return health_status

        # Check memory usage
        try:
            memory_info = await self.get_memory_usage()
            if "error" not in memory_info:
                used_memory = memory_info.get("used_memory", 0)
                max_memory = memory_info.get("maxmemory", 0)

                if max_memory > 0:
                    memory_percentage = (used_memory / max_memory) * 100
                    if memory_percentage > 90:
                        status = "critical"
                        message = f"Memory usage: {memory_percentage:.1f}%"
                    elif memory_percentage > 80:
                        status = "warning"
                        message = f"Memory usage: {memory_percentage:.1f}%"
                    else:
                        status = "healthy"
                        message = f"Memory usage: {memory_percentage:.1f}%"
                else:
                    status = "healthy"
                    message = "No memory limit set"

                health_status["checks"]["memory"] = {"status": status, "message": message}
            else:
                health_status["checks"]["memory"] = {"status": "error", "message": memory_info["error"]}
        except Exception as e:
            health_status["checks"]["memory"] = {"status": "error", "message": str(e)}

        # Check connected clients
        try:
            clients_info = await self.get_basic_info()
            connected_clients = clients_info.get("connected_clients", 0)

            if connected_clients > 100:
                status = "warning"
                message = f"High client count: {connected_clients}"
            else:
                status = "healthy"
                message = f"Client count: {connected_clients}"

            health_status["checks"]["clients"] = {"status": status, "message": message}
        except Exception as e:
            health_status["checks"]["clients"] = {"status": "error", "message": str(e)}

        # Determine overall status
        if any(check["status"] in ["critical", "unhealthy"] for check in health_status["checks"].values()):
            health_status["overall_status"] = "unhealthy"
        elif any(check["status"] == "warning" for check in health_status["checks"].values()):
            health_status["overall_status"] = "degraded"
        else:
            health_status["overall_status"] = "healthy"

        return health_status

    def print_info(self, info: Dict[str, Any], title: str):
        """Print information in a formatted way."""
        print(f"\n📋 {title}")
        print("=" * 50)

        if "error" in info:
            print(f"❌ Error: {info['error']}")
            return

        for key, value in info.items():
            if isinstance(value, dict):
                print(f"\n{key.upper()}:")
                for sub_key, sub_value in value.items():
                    print(f"  {sub_key}: {sub_value}")
            else:
                print(f"{key}: {value}")

    def print_health_status(self, health_status: Dict[str, Any]):
        """Print health check results."""
        print(f"\n🏥 Redis Health Check - {health_status['timestamp']}")
        print("=" * 60)

        status_emoji = {
            "healthy": "✅",
            "degraded": "⚠️",
            "unhealthy": "❌"
        }

        overall_status = health_status["overall_status"]
        print(f"Overall Status: {status_emoji.get(overall_status, '❓')} {overall_status.upper()}")

        print("\nDetailed Checks:")
        for check_name, check_result in health_status["checks"].items():
            status = check_result["status"]
            message = check_result["message"]
            emoji = status_emoji.get(status, "❓")
            print(f"  {emoji} {check_name}: {message}")


async def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Redis monitoring tool")
    parser.add_argument("--url", default="redis://localhost:6379", help="Redis URL")
    parser.add_argument("--info", action="store_true", help="Show basic information")
    parser.add_argument("--memory", action="store_true", help="Show memory usage")
    parser.add_argument("--keyspace", action="store_true", help="Show keyspace information")
    parser.add_argument("--slow-log", type=int, metavar="COUNT", help="Show slow log entries")
    parser.add_argument("--clients", action="store_true", help="Show connected clients")
    parser.add_argument("--health", action="store_true", help="Perform health check")
    parser.add_argument("--monitor", type=int, metavar="SECONDS", help="Monitor commands")
    parser.add_argument("--all", action="store_true", help="Show all information")

    args = parser.parse_args()

    monitor = RedisMonitor(args.url)

    if not await monitor.connect():
        return

    try:
        if args.all or args.info:
            info = await monitor.get_basic_info()
            monitor.print_info(info, "Basic Information")

        if args.all or args.memory:
            memory_info = await monitor.get_memory_usage()
            monitor.print_info(memory_info, "Memory Usage")

        if args.all or args.keyspace:
            keyspace_info = await monitor.get_keyspace_info()
            monitor.print_info(keyspace_info, "Keyspace Information")

        if args.slow_log is not None:
            slow_log = await monitor.get_slow_log(args.slow_log)
            monitor.print_info({"slow_log": slow_log}, f"Slow Log (last {args.slow_log} entries)")

        if args.all or args.clients:
            clients = await monitor.get_client_list()
            monitor.print_info({"clients": clients}, "Connected Clients")

        if args.all or args.health:
            health_status = await monitor.health_check()
            monitor.print_health_status(health_status)

        if args.monitor is not None:
            await monitor.monitor_commands(args.monitor)

        if not any([args.info, args.memory, args.keyspace, args.slow_log, args.clients, args.health, args.monitor, args.all]):
            # Default: show basic info and health check
            info = await monitor.get_basic_info()
            monitor.print_info(info, "Basic Information")

            health_status = await monitor.health_check()
            monitor.print_health_status(health_status)

    finally:
        await monitor.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
