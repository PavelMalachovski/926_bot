"""Tests for the five strategy-quality improvements."""

from datetime import datetime, timedelta, timezone

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.fvg import select_valid_fvg
from app.services.smc.journal import SignalJournal, evaluate_signal
from app.services.smc.models import (
    AnalysisResult,
    Direction,
    Trend,
    Verdict,
)
from app.services.smc.sessions import same_trading_day, session_end_utc
from app.services.smc.structure import detect_trend
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    candle,
    m5_long_trigger,
    make_candles,
)


def _fresh_result() -> AnalysisResult:
    return AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )


class TestH4Reclaim:
    """Item 3: a reclaimed fakeout below the last HL must not kill the trend."""

    def test_fakeout_reclaimed_keeps_uptrend(self):
        # One body close below the last HL (3119) immediately reclaimed.
        # (Once the dip survives long enough to confirm a lower pivot low,
        # the structure is legitimately broken — that case stays FLAT.)
        closes = H4_UPTREND_CLOSES + [3110, 3140]
        assert detect_trend(make_candles(closes)) == Trend.UP

    def test_break_still_held_is_flat(self):
        closes = H4_UPTREND_CLOSES + [3110, 3100, 3090, 3080]
        assert detect_trend(make_candles(closes)) == Trend.FLAT


class TestTpFallback:
    """Item 2: if the H1 target fails min RR, the H4 target is still valid."""

    def test_h4_target_used_when_h1_rr_too_low(self):
        # min_rr=7: H1 supply at 3200 gives RR ~5.3 (fails), H4 zone at 3250
        # gives RR ~9.6 (passes) -> trade approved with the H4 target.
        result = TripleSyncEngine(min_rr=7.0).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup.take_profit == 3250.0

    def test_h1_target_still_preferred_when_valid(self):
        result = TripleSyncEngine(min_rr=2.0).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.setup.take_profit == 3200.0


class TestCryptoSessionScope:
    """Item 4: crypto FVG stays valid across London->NY within one day."""

    @staticmethod
    def _cross_window_candles():
        # FVG formed in the London window (11:4x UTC), price checked in NY
        london = datetime(2026, 7, 6, 11, 40, tzinfo=timezone.utc)
        ny = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)
        return [
            candle(3130, 3132, 3128, 3131, start=london, index=0),
            candle(3131, 3140, 3130, 3139, start=london, index=1),
            candle(3139, 3143, 3138, 3142, start=london, index=2),  # gap 3132-3138
            candle(3142, 3144, 3140, 3143, start=ny, index=0),
            candle(3143, 3145, 3141, 3144, start=ny, index=1),
        ]

    def test_forex_scope_rejects_cross_session_fvg(self):
        candles = self._cross_window_candles()
        assert (
            select_valid_fvg(candles, Direction.LONG, 0, min_size=2.0) is None
        )

    def test_crypto_day_scope_accepts_it(self):
        candles = self._cross_window_candles()
        fvg = select_valid_fvg(
            candles, Direction.LONG, 0, min_size=2.0, same_day_scope=True
        )
        assert fvg is not None
        assert fvg.bottom == 3132.0 and fvg.top == 3138.0


class TestSessions:
    def test_same_trading_day(self):
        a = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
        b = datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc)
        c = datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc)  # 01:00 Prague next day
        assert same_trading_day(a, b)
        assert not same_trading_day(b, c)

    def test_session_end_utc(self):
        # 16:00 Prague summer (14:00 UTC) is in the NY window ending 20:00 Prague
        dt = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
        end = session_end_utc(dt)
        assert end == datetime(2026, 7, 6, 18, 0, tzinfo=timezone.utc)

    def test_trading_hours_08_20_prague(self):
        from app.services.smc.sessions import active_session

        def at(hour, minute=0, day=8):  # Wed 2026-07-08, CEST = UTC+2
            return datetime(2026, 7, day, hour - 2, minute, tzinfo=timezone.utc)

        assert active_session(at(7, 59)) is None
        assert active_session(at(8, 0)) == "Frankfurt/London"
        assert active_session(at(13, 59)) == "Frankfurt/London"
        assert active_session(at(14, 0)) == "New York"
        assert active_session(at(19, 59)) == "New York"
        assert active_session(at(20, 0)) is None

    def test_forex_weekends_off_crypto_on(self):
        from app.services.smc.sessions import active_session

        saturday = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)  # 10:00 Prague
        assert active_session(saturday) == "Frankfurt/London"  # crypto: every day
        assert active_session(saturday, require_weekday=True) is None  # forex: off
        friday = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)
        assert active_session(friday, require_weekday=True) == "Frankfurt/London"


class TestJournal:
    """Item 5: signal lifecycle and stats."""

    @staticmethod
    def _signal(status="pending", entry=100.0, sl=95.0, tp=110.0):
        created = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
        return {
            "id": "test",
            "pair": "ETHUSD",
            "direction": "long",
            "entry": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "rr": 2.0,
            "created_at": created.isoformat(),
            "expires_at": (created + timedelta(hours=6)).isoformat(),
            "status": status,
            "filled_at": None,
            "resolved_at": None,
            "checked_until": None,
        }

    @staticmethod
    def _c(index, o, h, low, c):
        start = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
        return candle(o, h, low, c, start=start, index=index)

    def test_fill_then_tp(self):
        signal = self._signal()
        candles = [
            self._c(1, 103, 104, 99.5, 101),  # touches entry 100 -> open
            self._c(2, 101, 105, 100.5, 104),
            self._c(3, 104, 111, 103, 110),  # hits TP 110
        ]
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        assert evaluate_signal(signal, candles, now)["status"] == "tp"

    def test_fill_then_sl(self):
        signal = self._signal()
        candles = [
            self._c(1, 103, 104, 99.5, 101),
            self._c(2, 101, 102, 94.5, 96),  # hits SL 95
        ]
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        assert evaluate_signal(signal, candles, now)["status"] == "sl"

    def test_same_candle_tp_and_sl_counts_sl(self):
        signal = self._signal(status="open")
        candles = [self._c(1, 100, 111, 94, 105)]  # touches both
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        assert evaluate_signal(signal, candles, now)["status"] == "sl"

    def test_pending_expires_after_session(self):
        signal = self._signal()
        candles = [self._c(1, 103, 104, 101, 102)]  # never touches entry
        now = datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc)  # past expiry
        assert evaluate_signal(signal, candles, now)["status"] == "expired"

    def test_incremental_check_does_not_rescan(self):
        signal = self._signal(status="open")
        candles = [self._c(1, 100, 101, 94, 96)]  # SL candle
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        evaluate_signal(signal, candles, now)
        assert signal["status"] == "sl"
        # watermark advanced past the candle
        assert signal["checked_until"] is not None

    def test_journal_records_and_reports(self, tmp_path):
        from app.services.smc.db import Database

        db = Database(str(tmp_path / "smc.db"))
        journal = SignalJournal(db)
        result = _fresh_result()
        result.session_name = "New York"
        engine = TripleSyncEngine(min_rr=2.0)
        result = engine.evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=result,
        )
        signal = journal.record(result)
        assert signal["status"] == "pending"
        assert "Total setups: 1" in journal.stats_text()

        reloaded = SignalJournal(Database(str(tmp_path / "smc.db")))
        assert len(reloaded.signals) == 1
