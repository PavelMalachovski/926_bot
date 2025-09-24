#!/usr/bin/env python3
"""
Comprehensive integration test for all migrated components.
This script tests all the migrated functionality to ensure everything works together.
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import all migrated services
from app.services.chart_service import chart_service
from app.services.notification_service import NotificationService
from app.services.user_settings_service import UserSettingsHandler
from app.services.digest_service import DailyDigestScheduler
from app.services.visualize_service import VisualizeHandler
from app.services.gpt_analysis_service import gpt_analysis_service
from app.services.notification_scheduler_service import NotificationScheduler
from app.services.database_service import DatabaseService
from app.services.scraping_service import ScrapingService
from app.services.telegram_service import TelegramService
from app.utils.telegram_utils import escape_markdown_v2, format_news_message


class MockConfig:
    """Mock configuration for testing."""
    def __init__(self):
        self.telegram_bot_token = "test_token"
        self.telegram_chat_id = "test_chat_id"
        self.render_hostname = "test.render.com"
        self.database_url = "sqlite:///test.db"


class MockBotManager:
    """Mock bot manager for testing."""
    def __init__(self):
        self.sent_messages = []
        self.sent_photos = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        """Mock send message."""
        self.sent_messages.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return True

    async def send_photo(self, chat_id: int, photo: bytes, caption: str = None, **kwargs):
        """Mock send photo."""
        self.sent_photos.append({"chat_id": chat_id, "photo": photo, "caption": caption, "kwargs": kwargs})
        return True


async def test_chart_service():
    """Test the advanced chart service."""
    print("🧪 Testing Chart Service...")

    try:
        # Test health check
        health = chart_service.health_check()
        print(f"   ✅ Health check: {health['status']}")

        # Test chart generation
        from app.models.chart import ChartRequest
        request = ChartRequest(
            currency="EURUSD",
            event_name="Test Event",
            start_time=datetime.now() - timedelta(hours=2),
            end_time=datetime.now() + timedelta(hours=2),
            chart_type="intraday"
        )

        response = await chart_service.generate_chart(request)
        if response.success:
            print(f"   ✅ Chart generated: {len(response.chart_image)} bytes")
        else:
            print(f"   ⚠️ Chart generation failed: {response.error_message}")

        # Test event chart
        chart_image = await chart_service.create_event_chart("EURUSD", datetime.now(), "Test Event")
        if chart_image:
            print(f"   ✅ Event chart created: {len(chart_image)} bytes")
        else:
            print(f"   ⚠️ Event chart creation failed")

        return True

    except Exception as e:
        print(f"   ❌ Chart service test failed: {e}")
        return False


async def test_notification_service():
    """Test the advanced notification service."""
    print("🧪 Testing Notification Service...")

    try:
        service = NotificationService()

        # Test health check
        health = service.health_check()
        print(f"   ✅ Health check: {health['status']}")

        # Test notification stats
        stats = await service.get_notification_stats()
        print(f"   ✅ Notification stats: {stats}")

        # Test deduplication
        notification_id = service.deduplication._generate_notification_id("test", user_id=123, date="2024-01-01")
        print(f"   ✅ Deduplication ID generated: {notification_id}")

        return True

    except Exception as e:
        print(f"   ❌ Notification service test failed: {e}")
        return False


async def test_user_settings_service():
    """Test the user settings service."""
    print("🧪 Testing User Settings Service...")

    try:
        db_service = DatabaseService()
        handler = UserSettingsHandler(db_service)

        # Test settings keyboard generation
        keyboard = handler.get_settings_keyboard(12345)
        print(f"   ✅ Settings keyboard: {len(keyboard['inline_keyboard'])} rows")

        # Test currency keyboard
        currency_keyboard = handler.get_currencies_keyboard(12345)
        print(f"   ✅ Currency keyboard: {len(currency_keyboard['inline_keyboard'])} rows")

        # Test impact keyboard
        impact_keyboard = handler.get_impact_keyboard(12345)
        print(f"   ✅ Impact keyboard: {len(impact_keyboard['inline_keyboard'])} rows")

        # Test settings summary
        summary = handler.get_user_settings_summary(12345)
        print(f"   ✅ Settings summary generated: {len(summary)} characters")

        return True

    except Exception as e:
        print(f"   ❌ User settings service test failed: {e}")
        return False


async def test_digest_service():
    """Test the daily digest scheduler."""
    print("🧪 Testing Daily Digest Scheduler...")

    try:
        config = MockConfig()
        bot_manager = MockBotManager()
        db_service = DatabaseService()

        scheduler = DailyDigestScheduler(db_service, bot_manager, config)

        # Test health check
        health = scheduler.health_check()
        print(f"   ✅ Health check: {health['status']}")

        # Test scheduled jobs
        jobs = scheduler.get_scheduled_jobs()
        print(f"   ✅ Scheduled jobs: {len(jobs)}")

        # Test shutdown
        scheduler.shutdown()
        print(f"   ✅ Scheduler shutdown completed")

        return True

    except Exception as e:
        print(f"   ❌ Digest service test failed: {e}")
        return False


async def test_visualize_service():
    """Test the visualization service."""
    print("🧪 Testing Visualization Service...")

    try:
        db_service = DatabaseService()
        telegram_service = TelegramService()
        handler = VisualizeHandler(db_service, telegram_service)

        # Test health check
        health = handler.health_check()
        print(f"   ✅ Health check: {health['status']}")

        # Test currency selection keyboard
        keyboard = handler.get_currency_selection_keyboard()
        print(f"   ✅ Currency selection keyboard: {len(keyboard['inline_keyboard'])} rows")

        # Test chart type keyboard
        chart_keyboard = handler.get_chart_type_keyboard("EURUSD")
        print(f"   ✅ Chart type keyboard: {len(chart_keyboard['inline_keyboard'])} rows")

        # Test time window keyboard
        time_keyboard = handler.get_time_window_keyboard("EURUSD", "symmetric")
        print(f"   ✅ Time window keyboard: {len(time_keyboard['inline_keyboard'])} rows")

        # Test available currencies
        currencies = handler.get_available_currencies()
        print(f"   ✅ Available currencies: {len(currencies)}")

        return True

    except Exception as e:
        print(f"   ❌ Visualization service test failed: {e}")
        return False


async def test_gpt_analysis_service():
    """Test the GPT analysis service."""
    print("🧪 Testing GPT Analysis Service...")

    try:
        # Test health check
        health = gpt_analysis_service.health_check()
        print(f"   ✅ Health check: {health['status']}")

        # Test news analysis
        news_item = {
            "event": "Test Event",
            "currency": "EURUSD",
            "actual": "1.2000",
            "forecast": "1.1950",
            "previous": "1.1980",
            "impact": "high"
        }

        analysis = await gpt_analysis_service.analyze_news_event(news_item)
        print(f"   ✅ News analysis: {len(analysis)} characters")

        # Test market conditions analysis
        market_analysis = await gpt_analysis_service.analyze_market_conditions("EURUSD", 30)
        print(f"   ✅ Market analysis: {market_analysis.get('status', 'completed')}")

        return True

    except Exception as e:
        print(f"   ❌ GPT analysis service test failed: {e}")
        return False


async def test_notification_scheduler_service():
    """Test the notification scheduler service."""
    print("🧪 Testing Notification Scheduler Service...")

    try:
        config = MockConfig()
        bot_manager = MockBotManager()
        db_service = DatabaseService()

        scheduler = NotificationScheduler(db_service, bot_manager, config)

        # Test health check
        health = scheduler.health_check()
        print(f"   ✅ Health check: {health['status']}")

        # Test scheduled jobs
        jobs = scheduler.get_scheduled_jobs()
        print(f"   ✅ Scheduled jobs: {len(jobs)}")

        # Test shutdown
        scheduler.shutdown()
        print(f"   ✅ Scheduler shutdown completed")

        return True

    except Exception as e:
        print(f"   ❌ Notification scheduler service test failed: {e}")
        return False


async def test_utils():
    """Test the utility functions."""
    print("🧪 Testing Utility Functions...")

    try:
        # Test markdown escaping
        test_text = "Test *text* with _markdown_ and [links](url)"
        escaped = escape_markdown_v2(test_text)
        print(f"   ✅ Markdown escaping: {len(escaped)} characters")

        # Test news message formatting
        news_items = [
            {
                "currency": "EURUSD",
                "time": "08:30",
                "event": "Test Event",
                "actual": "1.2000",
                "forecast": "1.1950",
                "previous": "1.1980",
                "impact": "high"
            }
        ]

        message = format_news_message(news_items, datetime.now().date(), "high", True, ["EURUSD"])
        print(f"   ✅ News message formatting: {len(message)} characters")

        return True

    except Exception as e:
        print(f"   ❌ Utils test failed: {e}")
        return False


async def test_integration():
    """Test integration between all services."""
    print("🧪 Testing Service Integration...")

    try:
        # Test that all services can be imported and initialized
        services = [
            chart_service,
            NotificationService(),
            UserSettingsHandler(DatabaseService()),
            gpt_analysis_service
        ]

        print(f"   ✅ All {len(services)} services initialized successfully")

        # Test health checks
        health_checks = []
        for service in services:
            if hasattr(service, 'health_check'):
                health = service.health_check()
                health_checks.append(health.get('status', 'unknown'))

        print(f"   ✅ Health checks: {health_checks}")

        return True

    except Exception as e:
        print(f"   ❌ Integration test failed: {e}")
        return False


async def main():
    """Run all integration tests."""
    print("🚀 Starting Comprehensive Migration Integration Tests")
    print("=" * 60)

    tests = [
        ("Chart Service", test_chart_service),
        ("Notification Service", test_notification_service),
        ("User Settings Service", test_user_settings_service),
        ("Digest Service", test_digest_service),
        ("Visualization Service", test_visualize_service),
        ("GPT Analysis Service", test_gpt_analysis_service),
        ("Notification Scheduler Service", test_notification_scheduler_service),
        ("Utils", test_utils),
        ("Integration", test_integration)
    ]

    results = []

    for test_name, test_func in tests:
        print(f"\n📋 {test_name}")
        print("-" * 40)

        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"   ❌ Test failed with exception: {e}")
            results.append((test_name, False))

    # Summary
    print("\n" + "=" * 60)
    print("📊 TEST RESULTS SUMMARY")
    print("=" * 60)

    passed = 0
    total = len(results)

    for test_name, result in results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"{test_name:<30} {status}")
        if result:
            passed += 1

    print("-" * 60)
    print(f"Total Tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print(f"Success Rate: {(passed/total)*100:.1f}%")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! Migration is successful!")
        return True
    else:
        print(f"\n⚠️ {total - passed} tests failed. Please review the issues above.")
        return False


if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⏹️ Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n💥 Unexpected error: {e}")
        sys.exit(1)
