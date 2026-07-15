"""Tests for the Forex Factory red-news filter."""

from datetime import datetime, timedelta, timezone

from app.services.smc.instruments import get_instrument
from app.services.smc.news import NewsCalendar, parse_feed, relevant_currencies

SAMPLE_FEED = [
    {
        "title": "CPI m/m",
        "country": "USD",
        "date": "2026-07-15T08:30:00-04:00",  # 12:30 UTC
        "impact": "High",
    },
    {
        "title": "BOE Gov Speaks",
        "country": "GBP",
        "date": "2026-07-15T16:00:00-04:00",  # 20:00 UTC
        "impact": "High",
    },
    {
        "title": "German ZEW",
        "country": "EUR",
        "date": "2026-07-15T05:00:00-04:00",
        "impact": "Medium",  # not red -> ignored
    },
]


def _calendar(before=60, after=15) -> NewsCalendar:
    cal = NewsCalendar(before_minutes=before, after_minutes=after)
    cal.events = parse_feed(SAMPLE_FEED)
    cal.fetched_at = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)
    return cal


class TestParsing:
    def test_only_high_impact_kept(self):
        events = parse_feed(SAMPLE_FEED)
        assert [e.currency for e in events] == ["USD", "GBP"]

    def test_time_converted_to_utc(self):
        events = parse_feed(SAMPLE_FEED)
        assert events[0].time == datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)


class TestRelevance:
    def test_forex_pair_uses_both_currencies(self):
        assert relevant_currencies(get_instrument("USDJPY")) == {"USD", "JPY"}
        assert relevant_currencies(get_instrument("GBPUSD")) == {"GBP", "USD"}

    def test_crypto_only_usd(self):
        assert relevant_currencies(get_instrument("ETHUSD")) == {"USD"}


class TestBlackout:
    def test_blocked_one_hour_before(self):
        cal = _calendar()
        now = datetime(2026, 7, 15, 11, 45, tzinfo=timezone.utc)  # 45m before CPI
        assert cal.blackout({"USD"}, now) is not None

    def test_blocked_shortly_after(self):
        cal = _calendar()
        now = datetime(2026, 7, 15, 12, 40, tzinfo=timezone.utc)  # 10m after CPI
        assert cal.blackout({"USD"}, now) is not None

    def test_free_outside_window(self):
        cal = _calendar()
        before = datetime(2026, 7, 15, 11, 20, tzinfo=timezone.utc)  # 70m before
        after = datetime(2026, 7, 15, 12, 50, tzinfo=timezone.utc)  # 20m after
        assert cal.blackout({"USD"}, before) is None
        assert cal.blackout({"USD"}, after) is None

    def test_crypto_ignores_non_usd_news(self):
        cal = _calendar()
        # 30 min before the GBP event: GBPUSD blocked, ETHUSD (USD-only) free
        now = datetime(2026, 7, 15, 19, 30, tzinfo=timezone.utc)
        assert cal.blackout(relevant_currencies(get_instrument("GBPUSD")), now)
        assert cal.blackout(relevant_currencies(get_instrument("ETHUSD")), now) is None

    def test_crypto_blocked_by_usd_news(self):
        cal = _calendar()
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # 30m before CPI
        assert cal.blackout(relevant_currencies(get_instrument("ETHUSD")), now)


class TestUpcomingAndDigest:
    def test_upcoming_horizon(self):
        cal = _calendar()
        now = datetime(2026, 7, 15, 12, 10, tzinfo=timezone.utc)
        soon = cal.upcoming({"USD", "GBP"}, timedelta(minutes=30), now)
        assert [e.title for e in soon] == ["CPI m/m"]

    def test_digest_lists_todays_events_in_prague_time(self):
        cal = _calendar()
        now = datetime(2026, 7, 15, 5, 30, tzinfo=timezone.utc)
        text = cal.digest_text({"USD", "GBP", "JPY"}, now)
        assert "CPI m/m" in text and "14:30" in text  # 12:30 UTC = 14:30 Prague
        assert "BOE Gov Speaks" in text and "22:00" in text
        assert "Блэкаут" in text

    def test_digest_when_quiet_day(self):
        cal = _calendar()
        now = datetime(2026, 7, 16, 5, 30, tzinfo=timezone.utc)  # next day
        assert "нет" in cal.digest_text({"USD"}, now)
