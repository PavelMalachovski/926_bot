"""Session hours in messages are read off `sessions.WINDOWS` (audit
2026-09-03).

sessions.py declares WINDOWS the single source of truth for the trading day,
yet three messages carried the hours as literals — the engine's off-session
reason, the digest's block titles and the bot's Telegram description — so a
change to WINDOWS would have left them lying.
"""

import asyncio
from datetime import datetime, time, timezone

from app.services.smc import engine as engine_module
from app.services.smc import sessions
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.models import Verdict
from app.services.smc.news import NewsCalendar, parse_feed
from app.services.smc.sessions import trading_hours_label, window_labels


class TestLabels:
    def test_the_shipped_trading_day(self):
        assert trading_hours_label() == "08:00-18:30"
        assert window_labels() == [
            ("Frankfurt/London", "08:00–14:00"),
            ("New York", "14:00–18:30"),
        ]

    def test_the_labels_follow_windows(self, monkeypatch):
        monkeypatch.setattr(
            sessions,
            "WINDOWS",
            [(time(9, 0), time(12, 0), "Early"), (time(12, 0), time(17, 0), "Late")],
        )
        assert trading_hours_label() == "09:00-17:00"
        assert window_labels() == [("Early", "09:00–12:00"), ("Late", "12:00–17:00")]


class TestConsumers:
    def test_the_off_session_reason_quotes_windows(self, monkeypatch):
        monkeypatch.setattr(
            sessions, "WINDOWS", [(time(9, 0), time(17, 0), "Only")]
        )

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                # 03:00 UTC = 05:00 Prague (CEST): outside the patched window
                return datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)

        monkeypatch.setattr(engine_module, "datetime", _Frozen)
        # Off-session returns before any fetch, so no fetcher is needed.
        result = asyncio.run(TripleSyncEngine(fetcher=object()).analyze())
        assert result.verdict == Verdict.OFF_SESSION
        assert "09:00-17:00 Prague" in result.reasons[0]
        assert "08:00" not in result.reasons[0]

    def test_the_digest_titles_quote_windows(self, monkeypatch):
        monkeypatch.setattr(
            sessions, "WINDOWS", [(time(9, 0), time(17, 0), "Frankfurt/London")]
        )
        cal = NewsCalendar()
        cal.events = parse_feed([{
            "title": "CPI m/m",
            "country": "USD",
            "date": "2026-07-15T08:30:00-04:00",  # 12:30 UTC = 14:30 Prague
            "impact": "High",
        }])
        cal.fetched_at = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)
        text = cal.digest_text(
            ["USDJPY"], datetime(2026, 7, 15, 5, 30, tzinfo=timezone.utc)
        )
        assert "🌅 <b>London 09:00–17:00</b>" in text
        assert "🔴 14:30 CPI m/m (USD)" in text
        assert "14:00–18:30" not in text and "New York" not in text
