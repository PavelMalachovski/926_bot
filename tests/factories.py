"""Test data factories using factory_boy."""

import factory
from factory import fuzzy
from datetime import datetime, time, timedelta
from typing import List, Dict, Any

from app.database.models import UserModel, ForexNewsModel, NotificationModel
from app.models.user import UserCreate, UserPreferences
from app.models.forex_news import ForexNewsCreate
from app.models.notification import NotificationCreate


class UserPreferencesFactory(factory.Factory):
    """Factory for UserPreferences."""

    class Meta:
        model = UserPreferences

    preferred_currencies = factory.List([
        fuzzy.FuzzyChoice(["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"])
        for _ in range(2)
    ])
    impact_levels = factory.List([
        fuzzy.FuzzyChoice(["high", "medium", "low"])
        for _ in range(2)
    ])
    analysis_required = fuzzy.FuzzyChoice([True, False])
    digest_time = factory.LazyFunction(lambda: time(8, 0))
    timezone = fuzzy.FuzzyChoice(["Europe/Prague", "America/New_York", "Asia/Tokyo"])
    notifications_enabled = fuzzy.FuzzyChoice([True, False])
    notification_minutes = fuzzy.FuzzyChoice([15, 30, 60])
    notification_impact_levels = factory.List([
        fuzzy.FuzzyChoice(["high", "medium", "low"])
        for _ in range(1)
    ])
    charts_enabled = fuzzy.FuzzyChoice([True, False])
    chart_type = fuzzy.FuzzyChoice(["single", "multi", "none"])
    chart_window_hours = fuzzy.FuzzyInteger(1, 6)


class UserCreateFactory(factory.Factory):
    """Factory for UserCreate."""

    class Meta:
        model = UserCreate

    telegram_id = factory.Sequence(lambda n: 100000000 + n)
    username = factory.Sequence(lambda n: f"user{n}")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    language_code = fuzzy.FuzzyChoice(["en", "cs", "de", "fr", "es"])
    is_bot = False
    is_premium = fuzzy.FuzzyChoice([True, False])
    preferences = factory.SubFactory(UserPreferencesFactory)


class UserModelFactory(factory.Factory):
    """Factory for UserModel."""

    class Meta:
        model = UserModel

    id = factory.Sequence(lambda n: n + 1)
    telegram_id = factory.Sequence(lambda n: 100000000 + n)
    username = factory.Sequence(lambda n: f"user{n}")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    language_code = fuzzy.FuzzyChoice(["en", "cs", "de", "fr", "es"])
    is_bot = False
    is_premium = fuzzy.FuzzyChoice([True, False])
    is_active = True

    # Individual preference fields (matching SQLAlchemy model)
    preferred_currencies = factory.List([
        fuzzy.FuzzyChoice(["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"])
        for _ in range(2)
    ])
    impact_levels = factory.List([
        fuzzy.FuzzyChoice(["high", "medium", "low"])
        for _ in range(2)
    ])
    analysis_required = fuzzy.FuzzyChoice([True, False])
    digest_time = factory.LazyFunction(lambda: "08:00:00")
    timezone = fuzzy.FuzzyChoice(["Europe/Prague", "America/New_York", "Asia/Tokyo"])
    notifications_enabled = fuzzy.FuzzyChoice([True, False])
    notification_minutes = fuzzy.FuzzyChoice([15, 30, 60])
    notification_impact_levels = factory.List([
        fuzzy.FuzzyChoice(["high", "medium", "low"])
        for _ in range(1)
    ])
    charts_enabled = fuzzy.FuzzyChoice([True, False])
    chart_type = fuzzy.FuzzyChoice(["single", "multi", "none"])
    chart_window_hours = fuzzy.FuzzyInteger(1, 6)

    created_at = factory.LazyFunction(datetime.utcnow)
    updated_at = factory.LazyFunction(datetime.utcnow)


class ForexNewsCreateFactory(factory.Factory):
    """Factory for ForexNewsCreate."""

    class Meta:
        model = ForexNewsCreate

    date = factory.LazyFunction(lambda: datetime.utcnow())
    time = factory.LazyFunction(lambda: "14:30")
    currency = fuzzy.FuzzyChoice(["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"])
    event = factory.Faker("sentence", nb_words=3)
    actual = factory.Faker("numerify", text="###.#")
    forecast = factory.Faker("numerify", text="###.#")
    previous = factory.Faker("numerify", text="###.#")
    impact_level = fuzzy.FuzzyChoice(["high", "medium", "low"])
    analysis = factory.Faker("text", max_nb_chars=200)


class ForexNewsModelFactory(factory.Factory):
    """Factory for ForexNewsModel."""

    class Meta:
        model = ForexNewsModel

    id = factory.Sequence(lambda n: n + 1)
    date = factory.LazyFunction(lambda: datetime.utcnow())
    time = factory.LazyFunction(lambda: "14:30")
    currency = fuzzy.FuzzyChoice(["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"])
    event = factory.Faker("sentence", nb_words=3)
    actual = factory.Faker("numerify", text="###.#")
    forecast = factory.Faker("numerify", text="###.#")
    previous = factory.Faker("numerify", text="###.#")
    impact_level = fuzzy.FuzzyChoice(["high", "medium", "low"])
    analysis = factory.Faker("text", max_nb_chars=200)
    source = factory.Faker("company")
    country = factory.Faker("country")
    event_type = fuzzy.FuzzyChoice(["economic", "political", "central_bank"])
    created_at = factory.LazyFunction(datetime.utcnow)
    updated_at = factory.LazyFunction(datetime.utcnow)


class NotificationCreateFactory(factory.Factory):
    """Factory for NotificationCreate."""

    class Meta:
        model = NotificationCreate

    user_id = factory.Sequence(lambda n: n + 1)
    event_id = factory.Sequence(lambda n: n + 1)
    notification_type = fuzzy.FuzzyChoice([
        "event_reminder", "event_start", "daily_digest", "analysis_complete"
    ])
    message = factory.Faker("sentence", nb_words=8)
    scheduled_time = factory.LazyFunction(
        lambda: datetime.utcnow() + timedelta(minutes=30)
    )
    status = fuzzy.FuzzyChoice(["pending", "sent", "failed", "cancelled"])


class NotificationModelFactory(factory.Factory):
    """Factory for NotificationModel."""

    class Meta:
        model = NotificationModel

    user_id = factory.Sequence(lambda n: n + 1)
    event_id = factory.Sequence(lambda n: n + 1)
    notification_type = fuzzy.FuzzyChoice([
        "event_reminder", "event_start", "daily_digest", "analysis_complete"
    ])
    message = factory.Faker("sentence", nb_words=8)
    scheduled_time = factory.LazyFunction(
        lambda: datetime.utcnow() + timedelta(minutes=30)
    )
    status = fuzzy.FuzzyChoice(["pending", "sent", "failed", "cancelled"])
    sent_at = factory.LazyFunction(lambda: datetime.utcnow())
    created_at = factory.LazyFunction(datetime.utcnow)
    updated_at = factory.LazyFunction(datetime.utcnow)


# Sample data generators for specific test scenarios
def create_sample_users(count: int = 5) -> List[Dict[str, Any]]:
    """Create sample users for testing."""
    return [UserCreateFactory.build() for _ in range(count)]


def create_sample_forex_news(count: int = 10) -> List[Dict[str, Any]]:
    """Create sample forex news for testing."""
    return [ForexNewsCreateFactory.build() for _ in range(count)]


def create_sample_notifications(count: int = 5) -> List[Dict[str, Any]]:
    """Create sample notifications for testing."""
    return [NotificationCreateFactory.build() for _ in range(count)]


def create_high_impact_news() -> Dict[str, Any]:
    """Create high impact forex news for testing."""
    return ForexNewsCreateFactory.build(
        impact_level="high",
        currency="USD",
        event="Non-Farm Payrolls"
    )


def create_user_with_preferences(**kwargs) -> Dict[str, Any]:
    """Create user with specific preferences for testing."""
    preferences = UserPreferencesFactory.build(**kwargs.get('preferences', {}))
    return UserCreateFactory.build(preferences=preferences, **kwargs)
