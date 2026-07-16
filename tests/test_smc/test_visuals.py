"""Tests for the visual upgrade pack: DB migration, trade marks, discipline,
live-card events, chart rendering, pretty stats, digest timeline."""

import sqlite3
from datetime import datetime, timedelta, timezone

from app.services.smc.db import Database
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.journal import SignalJournal
from app.services.smc.models import AnalysisResult, Verdict
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    candle,
    m5_long_trigger,
    make_candles,
)

NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)


def _approved_result() -> AnalysisResult:
    result = AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )
    result.session_name = "New York"
    result = TripleSyncEngine(min_rr=2.0).evaluate(
        h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5=m5_long_trigger(),
        result=result,
    )
    result.m5_candles = m5_long_trigger()
    assert result.verdict == Verdict.APPROVED_LIMIT
    return result


class TestDbMigration:
    def test_old_schema_gets_new_columns(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT)"""
        )
        conn.commit()
        conn.close()
        db = Database(path)
        columns = {r["name"] for r in db.conn.execute("PRAGMA table_info(signals)")}
        assert {"taken", "message_id", "alert_text"} <= columns


class TestTradeMarksAndDiscipline:
    @staticmethod
    def _journal(tmp_path) -> SignalJournal:
        return SignalJournal(Database(str(tmp_path / "j.db")))

    @staticmethod
    def _sl_signal(journal, pair="ETHUSD", direction="long", taken=1):
        signal = {
            "id": f"sig{len(journal.signals)}",
            "pair": pair,
            "direction": direction,
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "rr": 2.0,
            "session": "New York",
            "created_at": NOW.isoformat(),
            "expires_at": None,
            "status": "sl",
            "filled_at": NOW.isoformat(),
            "resolved_at": NOW.isoformat(),
            "checked_until": None,
            "taken": taken,
            "message_id": None,
            "alert_text": None,
        }
        journal.signals.append(signal)
        journal.save()
        return signal

    def test_mark_taken_persists(self, tmp_path):
        journal = self._journal(tmp_path)
        signal = self._sl_signal(journal, taken=None)
        journal.mark_taken(signal["id"], True)
        reloaded = SignalJournal(journal.db)
        assert reloaded.get(signal["id"])["taken"] == 1

    def test_rule_10_reentry_ban(self, tmp_path):
        journal = self._journal(tmp_path)
        self._sl_signal(journal, pair="USDJPY", direction="long")
        block = journal.discipline_block("USDJPY", "long", "New York", NOW)
        assert block and "Rule 10" in block
        # different direction or pair is not blocked
        assert journal.discipline_block("USDJPY", "short", "New York", NOW) is None
        assert journal.discipline_block("GBPUSD", "long", "New York", NOW) is None

    def test_rule_02_two_stops_close_the_day(self, tmp_path):
        journal = self._journal(tmp_path)
        self._sl_signal(journal, pair="USDJPY")
        self._sl_signal(journal, pair="GBPUSD")
        block = journal.discipline_block("ETHUSD", "long", "New York", NOW)
        assert block and "Rule 0.2" in block

    def test_skipped_stops_do_not_count(self, tmp_path):
        journal = self._journal(tmp_path)
        self._sl_signal(journal, pair="USDJPY", taken=0)
        self._sl_signal(journal, pair="GBPUSD", taken=None)
        assert journal.discipline_block("ETHUSD", "long", "New York", NOW) is None


class TestLiveCardEvents:
    def test_update_pair_reports_fill_and_tp(self, tmp_path):
        journal = SignalJournal(Database(str(tmp_path / "j.db")))
        result = _approved_result()
        signal = journal.record(result)
        start = datetime(2026, 7, 6, 15, 45, tzinfo=timezone.utc)
        candles = [
            candle(3142, 3143, 3139.0, 3141, start=start, index=0),  # fills 3139.5
            candle(3141, 3205, 3140, 3201, start=start, index=1),  # hits TP 3200
        ]
        events = journal.update_pair("ETHUSD", candles)
        assert [e for _, e in events] == ["filled", "tp"]
        assert signal["status"] == "tp"


class TestChart:
    def test_renders_png(self):
        from app.services.smc.chart import render_setup_chart

        png = render_setup_chart(_approved_result())
        assert png is not None and png[:4] == b"\x89PNG"

    def test_returns_none_without_candles(self):
        from app.services.smc.chart import render_setup_chart

        result = _approved_result()
        result.m5_candles = None
        assert render_setup_chart(result) is None


class TestPrettyStats:
    def test_stats_contains_bars_and_sparkline(self, tmp_path):
        journal = SignalJournal(Database(str(tmp_path / "j.db")))
        TestTradeMarksAndDiscipline._sl_signal(journal, pair="ETHUSD")
        tp = TestTradeMarksAndDiscipline._sl_signal(journal, pair="USDJPY")
        tp["status"] = "tp"
        journal.save()
        text = journal.stats_text()
        assert "▰" in text and "🟥" in text and "🟩" in text
        assert "marked taken: 2" in text


class TestDigestBlocks:
    def test_digest_shows_session_block_and_no_entry_window(self):
        from app.services.smc.news import NewsCalendar, parse_feed

        cal = NewsCalendar()
        cal.events = parse_feed(
            [
                {
                    "title": "CPI m/m",
                    "country": "USD",
                    "date": "2026-07-16T08:30:00-04:00",  # 14:30 Prague
                    "impact": "High",
                }
            ]
        )
        cal.fetched_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        text = cal.digest_text(
            ["ETHUSD"], datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
        )
        assert "New York 14–20" in text
        assert "🔴 14:30 CPI m/m (USD) → ETHUSD" in text
        assert "⛔ no entries 13:30–14:45" in text
