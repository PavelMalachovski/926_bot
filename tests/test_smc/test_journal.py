"""Phase 2 sniper redesign: journal partial-close lifecycle.

`evaluate_signal` gains a hybrid state machine (pending -> open ->
[sl | open_runner] -> [tp1_be | tp1_runner]) that only engages when the
signal carries `tp1`/`runner_tp` (actual price levels the engine now
computes for every approved setup — see engine.py). A signal without those
fields (a legacy DB row, or a hand-built test fixture that omits them) keeps
the plain pending/open/tp/sl/expired/timeout lifecycle byte-for-byte.

The seven scenarios below port the validated `sn_exit.py` replay harness
(C:\\temp\\926_bot_data\\scripts\\sn_exit.py, read-only reference) onto
`evaluate_signal`, plus the ONE deliberate behavior change from that harness:
a timeout while the runner leg is still open (TP1 already banked) resolves
as "tp1_be" (+1R), not a bare "timeout" that would silently erase the
banked R.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.db import Database
from app.services.smc.journal import OPEN_TIMEOUT, SignalJournal, evaluate_signal
from app.services.smc.models import (
    AnalysisResult,
    Direction,
    FVG,
    TradeSetup,
    Verdict,
    Zone,
)
from tests.test_smc.helpers import SESSION_BASE, candle

CREATED = SESSION_BASE  # 2026-07-06 14:00 UTC


def _approved_result_with_setup(**setup_overrides):
    """An AnalysisResult carrying a hand-built TradeSetup (Task 2 fields:
    tp1/runner_tp/tier_star), independent of the real engine — record()
    should persist whatever the setup carries without needing a full
    engine.evaluate() run."""
    now = datetime.now(tz=timezone.utc)
    result = AnalysisResult(symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT, checked_at=now)
    result.session_name = "New York"
    defaults = dict(
        direction=Direction.LONG, entry=100.0, stop_loss=90.0,
        take_profit=140.0, rr=4.0, fvg=FVG(0, 98.0, 100.0, True, now),
        tp1=120.0, runner_tp=130.0, tier_star=True,
    )
    defaults.update(setup_overrides)
    result.setup = TradeSetup(**defaults)
    return result


def _hybrid_signal(
    status="pending",
    entry=100.0,
    sl=90.0,
    tp1=120.0,
    runner_tp=130.0,
    direction="long",
    created_at=CREATED,
    expires_at=None,
    checked_until=None,
):
    """A signal carrying tp1/runner_tp — engages evaluate_signal's hybrid
    partial-close branch, mirroring what SignalJournal.record() now persists
    for every setup the real engine approves (Task 2 computes tp1/runner_tp
    unconditionally)."""
    return {
        "id": "hy",
        "pair": "ETHUSD",
        "direction": direction,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": 999.0,  # unused by the hybrid branch; present like a real row
        "rr": 2.0,
        "session": "New York",
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "status": status,
        "filled_at": None,
        "resolved_at": None,
        "checked_until": checked_until.isoformat() if checked_until else None,
        "taken": None,
        "message_id": None,
        "alert_text": None,
        "profile_key": "conservative",
        "tp1": tp1,
        "runner_tp": runner_tp,
        "tier": "regular",
        "result_r": None,
    }


def _c(index, o, h, low, c, start=CREATED):
    return candle(o, h, low, c, start=start, index=index)


class TestHybridLifecycle:
    """entry=100, sl=90 -> risk=10; tp1=120 (2R), runner_tp=130 (3R). BE
    banks 0.5*2R=+1.0R; runner banks 0.5*2R + 0.5*3R=+2.5R."""

    def test_full_stop_before_tp1(self):
        """Scenario 1 (sn_exit): SL hit before TP1 -> -1.0R."""
        signal = _hybrid_signal(status="open")
        candles = [_c(1, 100, 101, 89.5, 90)]  # hits SL 90
        now = CREATED + timedelta(hours=1)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "sl"
        assert result["result_r"] == -1.0

    def test_tp1_then_be(self):
        """Scenario 2 (sn_exit): TP1 tagged, then the NEXT candle touches
        entry -> tp1_be, +1.0R. BE is not judged on the TP1 candle itself."""
        signal = _hybrid_signal(status="open")
        candles = [
            _c(1, 110, 121, 109, 120),  # tags TP1 120 -> open_runner
            _c(2, 120, 122, 99.5, 100),  # next candle: touches entry -> BE
        ]
        now = CREATED + timedelta(hours=1)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "tp1_be"
        assert result["result_r"] == pytest.approx(1.0)

    def test_tp1_then_runner(self):
        """Scenario 3 (sn_exit): TP1 tagged, then the runner leg hits its
        target -> tp1_runner, +2.5R (default tp1_r=2.0/runner_r=3.0)."""
        signal = _hybrid_signal(status="open")
        candles = [
            _c(1, 110, 121, 109, 120),  # tags TP1 120 -> open_runner
            _c(2, 120, 131, 125, 130),  # runner 130, stays above entry
        ]
        now = CREATED + timedelta(hours=1)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "tp1_runner"
        assert result["result_r"] == pytest.approx(2.5)

    def test_tp1_and_sl_same_candle_is_sl(self):
        """Scenario 4 (sn_exit): a candle spanning both SL and TP1 resolves
        adverse-first -> sl, -1.0R, never open_runner."""
        signal = _hybrid_signal(status="open")
        candles = [_c(1, 100, 125, 89, 121)]  # low <= 90 AND high >= 120
        now = CREATED + timedelta(hours=1)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "sl"
        assert result["result_r"] == -1.0

    def test_runner_and_be_same_candle_is_be(self):
        """Scenario 5 (sn_exit): after TP1, a candle spanning both the
        runner target and the entry (BE) resolves adverse-first -> tp1_be,
        +1.0R, never tp1_runner."""
        signal = _hybrid_signal(status="open")
        candles = [
            _c(1, 110, 121, 109, 120),  # tags TP1 -> open_runner
            _c(2, 120, 131, 99, 130),  # low <= 100 (BE) AND high >= 130 (runner)
        ]
        now = CREATED + timedelta(hours=1)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "tp1_be"
        assert result["result_r"] == pytest.approx(1.0)

    def test_pending_expiry(self):
        """Scenario 6 (sn_exit): entry never touched before the session
        window's expiry -> expired, 0.0R."""
        signal = _hybrid_signal(
            status="pending", expires_at=CREATED + timedelta(hours=2)
        )
        candles = [_c(1, 105, 106, 102, 104)]  # never touches entry 100
        now = CREATED + timedelta(hours=3)  # past expiry
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "expired"
        assert result["result_r"] == 0.0

    def test_signal_candle_watermark(self):
        """Scenario 7 (sn_exit): the candle that produced the signal cannot
        also fill it — created_at is that candle's CLOSE time, so its
        candle_end equals the watermark and the candle is skipped even
        though its wick touches the entry."""
        signal_candle = candle(100, 101, 95, 100, start=CREATED, index=0)
        created_at = signal_candle.timestamp + timedelta(minutes=5)  # its close
        signal = _hybrid_signal(status="pending", created_at=created_at)
        now = created_at + timedelta(hours=1)
        result = evaluate_signal(signal, [signal_candle], now)
        assert result["status"] == "pending"  # not filled

    def test_timeout_after_tp1_resolves_be(self):
        """Phase 2 fix (THE deliberate divergence from sn_exit.py): TP1 is
        already banked when the runner leg times out (OPEN_TIMEOUT) — resolve
        as tp1_be (+1.0R), not a bare "timeout" that would erase the banked R."""
        signal = _hybrid_signal(status="open")
        candles = [_c(1, 110, 121, 109, 120)]  # tags TP1 -> open_runner, no more candles
        now = CREATED + OPEN_TIMEOUT + timedelta(minutes=1)  # timed out on the runner leg
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "tp1_be"
        assert result["result_r"] == pytest.approx(1.0)

    def test_timeout_before_tp1_is_a_bare_timeout(self):
        """Contrast case: a signal that never reaches TP1 still times out
        the old way (0.0R) — only a timeout AFTER TP1 gets the BE fix."""
        signal = _hybrid_signal(status="open")
        candles = [_c(1, 100, 105, 99, 101)]  # neither SL nor TP1
        now = CREATED + OPEN_TIMEOUT + timedelta(minutes=1)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "timeout"
        assert result["result_r"] == 0.0

    def test_timeout_after_tp1_logs_distinctly(self, monkeypatch):
        """The timeout-from-runner path must be observable in the logs, not
        just silently reclassified."""
        import app.services.smc.journal as journal_mod

        events = []

        class _Spy:
            def info(self, event, **kw):
                events.append(event)

            def error(self, event, **kw):
                pass

        monkeypatch.setattr(journal_mod, "logger", _Spy())
        signal = _hybrid_signal(status="open")
        candles = [_c(1, 110, 121, 109, 120)]
        now = CREATED + OPEN_TIMEOUT + timedelta(minutes=1)
        evaluate_signal(signal, candles, now)
        assert any("timeout" in e.lower() and "runner" in e.lower() for e in events)


class TestLegacySignalsUnchanged:
    """A signal without tp1/runner_tp (legacy DB row or a fixture that omits
    them) must keep today's exact pending/open/tp/sl/expired/timeout
    behavior — the hybrid branch never engages. Same fixtures as
    tests/test_smc/test_improvements.py::TestJournal (regression: byte-for-
    byte identical outcomes)."""

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

    def test_fill_then_tp_unchanged(self):
        signal = self._signal()
        assert signal.get("tp1") is None  # no hybrid fields -> legacy path
        candles = [
            self._c(1, 103, 104, 99.5, 101),  # touches entry 100 -> open
            self._c(2, 101, 105, 100.5, 104),
            self._c(3, 104, 111, 103, 110),  # hits TP 110
        ]
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "tp"
        assert "result_r" not in result  # untouched — legacy path never sets it

    def test_fill_then_sl_unchanged(self):
        signal = self._signal()
        candles = [
            self._c(1, 103, 104, 99.5, 101),
            self._c(2, 101, 102, 94.5, 96),  # hits SL 95
        ]
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        assert evaluate_signal(signal, candles, now)["status"] == "sl"

    def test_same_candle_tp_and_sl_counts_sl_unchanged(self):
        signal = self._signal(status="open")
        candles = [self._c(1, 100, 111, 94, 105)]  # touches both
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        assert evaluate_signal(signal, candles, now)["status"] == "sl"

    def test_pending_expires_after_session_unchanged(self):
        signal = self._signal()
        candles = [self._c(1, 103, 104, 101, 102)]  # never touches entry
        now = datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc)  # past expiry
        result = evaluate_signal(signal, candles, now)
        assert result["status"] == "expired"
        assert "result_r" not in result

    def test_open_times_out_unchanged(self):
        """Legacy timeout must stay a bare "timeout", never tp1_be — the
        Phase 2 BE-on-timeout fix is hybrid-only."""
        signal = self._signal(status="open")
        now = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc) + OPEN_TIMEOUT + \
            timedelta(minutes=1)
        result = evaluate_signal(signal, [], now)
        assert result["status"] == "timeout"
        assert "result_r" not in result


class TestRecordPersistsHybridFields:
    def test_record_persists_tp1_runner_tp_and_star_tier(self, tmp_path):
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        result = _approved_result_with_setup(tier_star=True)
        signal = journal.record(result)
        assert signal["tp1"] == 120.0
        assert signal["runner_tp"] == 130.0
        assert signal["tier"] == "star"
        assert signal["result_r"] is None  # unresolved yet

        reloaded = SignalJournal(Database(str(tmp_path / "j.db")))
        row = reloaded.get(signal["id"])
        assert row["tp1"] == 120.0
        assert row["runner_tp"] == 130.0
        assert row["tier"] == "star"

    def test_record_persists_regular_tier(self, tmp_path):
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        result = _approved_result_with_setup(tier_star=False)
        signal = journal.record(result)
        assert signal["tier"] == "regular"


class TestStatsCountsHybridWins:
    def _journal(self, tmp_path):
        return SignalJournal(Database(str(tmp_path / "stats.db")))

    @staticmethod
    def _row(pair, status, tier="regular", result_r=None, taken=None):
        now = datetime.now(tz=timezone.utc)
        return {
            "id": f"{pair}-{status}-{tier}-{result_r}",
            "pair": pair,
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 90.0,
            "take_profit": 140.0,
            "rr": 4.0,
            "session": "New York",
            "created_at": now.isoformat(),
            "expires_at": None,
            "status": status,
            "filled_at": None,
            "resolved_at": now.isoformat() if status not in ("pending", "open") else None,
            "checked_until": None,
            "taken": taken,
            "message_id": None,
            "alert_text": None,
            "profile_key": "conservative",
            "tp1": 120.0,
            "runner_tp": 130.0,
            "tier": tier,
            "result_r": result_r,
        }

    def test_tp1_be_and_tp1_runner_count_as_wins(self, tmp_path):
        journal = self._journal(tmp_path)
        journal.signals = [
            self._row("ETHUSD", "tp1_be", result_r=1.0),
            self._row("ETHUSD", "tp1_runner", result_r=2.5),
            self._row("ETHUSD", "sl", result_r=-1.0),
        ]
        journal.save()
        text = journal.stats_text(days=36500)
        assert "🎯 TP 2 | 🛑 SL 1" in text
        assert "Realized R" in text

    def test_per_pair_star_line_shows_negative_performance(self, tmp_path):
        """Spec watch item: a pair whose star-tier signals are net negative
        (e.g. USDJPY) must stay visible in /stats, not get averaged away by
        the regular-tier signals of the same pair."""
        journal = self._journal(tmp_path)
        journal.signals = [
            self._row("USDJPY", "sl", tier="star", result_r=-1.0),
            self._row("USDJPY", "sl", tier="star", result_r=-1.0),
            self._row("USDJPY", "tp1_runner", tier="regular", result_r=2.5),
        ]
        journal.save()
        text = journal.stats_text(days=36500)
        assert "⭐ Star tier by pair" in text
        assert "USDJPY: 2 resolved, 0W, -2.0R" in text

    def test_star_line_omitted_when_no_resolved_star_signals(self, tmp_path):
        journal = self._journal(tmp_path)
        journal.signals = [self._row("ETHUSD", "pending", tier="star", result_r=None)]
        journal.save()
        text = journal.stats_text(days=36500)
        assert "⭐ Star tier by pair" not in text


class TestRecordPersistsZoneKind:
    """Range trading (Task 5): record() carries `result.h1_zone.kind`
    ("OB", "FVG" or "RANGE") onto the persisted row, so /stats can later
    separate range setups from trend setups."""

    @pytest.mark.parametrize("kind", ["OB", "FVG", "RANGE"])
    def test_record_persists_zone_kind(self, tmp_path, kind):
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        result = _approved_result_with_setup()
        result.h1_zone = Zone(
            bottom=95.0,
            top=99.0,
            is_demand=True,
            pivot_index=0,
            timestamp=datetime.now(tz=timezone.utc),
            kind=kind,
        )
        signal = journal.record(result)
        assert signal["zone_kind"] == kind

        reloaded = SignalJournal(Database(str(tmp_path / "j.db")))
        row = reloaded.get(signal["id"])
        assert row["zone_kind"] == kind

    def test_record_persists_none_when_no_h1_zone(self, tmp_path):
        """Defensive: record() must not crash if h1_zone is somehow unset."""
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        result = _approved_result_with_setup()
        result.h1_zone = None
        signal = journal.record(result)
        assert signal["zone_kind"] is None


class TestZoneKindLegacyRowSafety:
    """A signal recorded before this column existed reads zone_kind as
    NULL/missing. Neither the lifecycle tracker nor /stats may assume a
    value is present."""

    def test_evaluate_signal_ignores_missing_zone_kind(self):
        signal = _hybrid_signal(status="open")
        assert "zone_kind" not in signal  # legacy shape: key absent entirely
        result = evaluate_signal(
            signal, [_c(1, 100, 101, 89.5, 90)], datetime.now(tz=timezone.utc)
        )
        assert result["status"] == "sl"

    def test_stats_text_ignores_null_zone_kind(self, tmp_path):
        journal = SignalJournal(Database(str(tmp_path / "s.db")))
        row = TestStatsCountsHybridWins._row("ETHUSD", "tp", result_r=1.0)
        row["zone_kind"] = None
        journal.signals = [row]
        journal.save()
        text = journal.stats_text(days=36500)
        assert "🎯 TP 1" in text


class TestRangeSignalsAreNotHybrid:
    """D14 (owner decision 2026-08-18): a range setup has no hybrid exit —
    one target, the opposite boundary, full size. The engine stops writing
    tp1/runner_tp for those, and `evaluate_signal` must track them on
    `take_profit` like any other non-hybrid signal.
    """

    def test_a_recorded_range_setup_carries_no_hybrid_levels(self, tmp_path):
        journal = SignalJournal(Database(str(tmp_path / "j.db")))
        result = _approved_result_with_setup(tp1=None, runner_tp=None)
        result.h1_zone = Zone(
            bottom=95.0, top=99.0, is_demand=True, pivot_index=0,
            timestamp=datetime.now(tz=timezone.utc), kind="RANGE",
        )
        signal = journal.record(result)
        assert signal["tp1"] is None and signal["runner_tp"] is None
        assert signal["zone_kind"] == "RANGE"

    def test_it_resolves_on_take_profit_not_on_tp1(self):
        """The plain path: price reaches `take_profit` (the opposite
        boundary) and the signal closes as a full "tp"."""
        signal = _hybrid_signal(status="open", tp1=None, runner_tp=None)
        signal["take_profit"] = 140.0
        signal["zone_kind"] = "RANGE"
        result = evaluate_signal(
            signal, [_c(1, 100, 141, 99, 140)], datetime.now(tz=timezone.utc)
        )
        assert result["status"] == "tp"

    def test_a_row_already_stored_with_hybrid_levels_takes_the_plain_path(self):
        """Rows written before D14 still carry the tp1/runner_tp the engine
        used to compute. `zone_kind` is what disqualifies them: tracking a
        range signal on TP1 would chase a price outside the box it aims at
        (here TP1 120.0 with the opposite boundary at 110.0)."""
        signal = _hybrid_signal(status="open", tp1=120.0, runner_tp=130.0)
        signal["take_profit"] = 110.0
        signal["zone_kind"] = "RANGE"
        result = evaluate_signal(
            signal, [_c(1, 100, 111, 99, 110)], datetime.now(tz=timezone.utc)
        )
        assert result["status"] == "tp"  # not "open_runner"
        assert result.get("tp1_at") is None

    def test_a_trend_row_with_the_same_levels_still_goes_hybrid(self):
        """The control: only zone_kind == "RANGE" leaves the hybrid path."""
        signal = _hybrid_signal(status="open", tp1=120.0, runner_tp=130.0)
        signal["take_profit"] = 110.0
        signal["zone_kind"] = "OB"
        result = evaluate_signal(
            signal, [_c(1, 100, 121, 99, 120)], CREATED + timedelta(minutes=30)
        )
        assert result["status"] == "open_runner"
        assert result["tp1_at"] is not None
