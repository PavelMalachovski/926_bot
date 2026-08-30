"""Backtester tests: loop fidelity, cache, metrics. Network-free.

The engine's own correctness lives in the engine suite — here a stub engine
owns the loop mechanics (windows, sessions, dedup, journal outcomes), plus
one smoke test that drives the real engine over synthetic candles.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.backtest import (
    H1_WINDOW,
    H4_WINDOW,
    M5_WINDOW,
    BacktestRun,
    SignalRecord,
    Stats,
    _window_end,
    compute_stats,
    one_away_from_star,
    render_combined,
    render_report,
    run_backtest,
    star_miss_autopsy,
    warning_autopsy,
)
from app.services.smc.db import Database
from app.services.smc.history import (
    cache_path,
    load_cache,
    load_history,
    merge_candles,
    save_cache,
)
from app.services.smc.journal import SignalJournal
from app.services.smc.models import (
    FVG,
    Candle,
    Direction,
    TradeSetup,
    Verdict,
    Zone,
)
from app.services.smc.profiles import CONSERVATIVE

from .helpers import SESSION_BASE, make_candles

# Monday 2026-07-06 14:00 UTC = 16:00 Prague — NY block, session ends 16:30 UTC.
_M5 = timedelta(minutes=5)


def _mk(ts: datetime, o=100.0, h=101.0, low=99.0, c=100.5) -> Candle:
    return Candle(timestamp=ts, open=o, high=h, low=low, close=c, volume=1.0)


def _journal(tmp_path) -> SignalJournal:
    return SignalJournal(Database(str(tmp_path / "bt.db")))


class _StubEngine:
    """Approves a scripted setup when the cycle's `now` matches a plan key;
    records every view it was shown so window tests can assert on them."""

    def __init__(self, plans=None):
        self.profile = CONSERVATIVE
        self.plans = plans or {}
        self.seen = []  # (now, h4_view, h1_view, m5_view)

    def evaluate(self, h4, h1, m5, result):
        self.seen.append((result.checked_at, h4, h1, m5))
        plan = self.plans.get(result.checked_at)
        if plan is None:
            result.verdict = Verdict.WATCH
            return result
        result.verdict = Verdict.APPROVED_LIMIT
        result.direction_source = "h4"
        result.h1_zone = Zone(
            bottom=plan["zone"][0],
            top=plan["zone"][1],
            is_demand=plan["direction"] == "long",
            pivot_index=1,
            timestamp=result.checked_at,
        )
        result.warnings = list(plan.get("warnings", []))
        missed = list(plan.get("missed", []))
        result.setup = TradeSetup(
            direction=Direction(plan["direction"]),
            entry=plan["entry"],
            stop_loss=plan["sl"],
            take_profit=plan.get("tp"),
            rr=plan.get("rr", 2.0),
            fvg=FVG(1, plan["entry"] - 1, plan["entry"], True, result.checked_at),
            tier_star=not missed,
            tier_missed=missed,
        )
        return result


class TestClosedCandleWindows:
    def test_window_end_is_strict_about_the_still_open_candle(self):
        h1 = [_mk(SESSION_BASE - timedelta(hours=2)), _mk(SESSION_BASE - timedelta(hours=1))]
        # At 14:00 both are closed; a minute earlier the 13:00 candle is not.
        assert _window_end(h1, SESSION_BASE, timedelta(hours=1)) == 2
        assert _window_end(h1, SESSION_BASE - timedelta(minutes=1), timedelta(hours=1)) == 1

    def test_engine_never_sees_an_unclosed_higher_timeframe_candle(self, tmp_path):
        m5 = [_mk(SESSION_BASE + _M5 * i) for i in range(12)]
        h1 = [_mk(SESSION_BASE - timedelta(hours=3) + timedelta(hours=i)) for i in range(4)]
        h4 = [_mk(SESSION_BASE - timedelta(hours=16) + timedelta(hours=4 * i)) for i in range(4)]
        engine = _StubEngine()
        run_backtest(
            "ETHUSD", h4, h1, m5,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=_journal(tmp_path),
            engine=engine,
            require_full_windows=False,
        )
        assert engine.seen, "in-session cycles must reach the engine"
        for now, h4_view, h1_view, m5_view in engine.seen:
            assert all(c.timestamp + timedelta(hours=4) <= now for c in h4_view)
            assert all(c.timestamp + timedelta(hours=1) <= now for c in h1_view)
            assert all(c.timestamp + _M5 <= now for c in m5_view)
            # The live fetchers' windows are the ceiling, never exceeded.
            assert len(h4_view) <= H4_WINDOW
            assert len(h1_view) <= H1_WINDOW
            assert len(m5_view) <= M5_WINDOW

    def test_off_session_candles_never_reach_the_engine(self, tmp_path):
        # 05:00 UTC = 07:00 Prague — before the 08:00 open, even for crypto.
        night = SESSION_BASE.replace(hour=5)
        m5 = [_mk(night + _M5 * i) for i in range(3)]
        engine = _StubEngine()
        run = run_backtest(
            "ETHUSD", [], [], m5,
            start=night,
            end=night + timedelta(hours=1),
            journal=_journal(tmp_path),
            engine=engine,
            require_full_windows=False,
        )
        assert engine.seen == []
        assert run.off_session == 3

    def test_warmup_cycles_are_skipped_until_windows_fill(self, tmp_path):
        m5 = [_mk(SESSION_BASE + _M5 * i) for i in range(3)]
        engine = _StubEngine()
        run = run_backtest(
            "ETHUSD", [], [], m5,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=_journal(tmp_path),
            engine=engine,
            require_full_windows=True,
        )
        assert engine.seen == []
        assert run.warmup_skipped == 3


class TestLoopRecordsAndOutcomes:
    def _plan(self, **extra):
        plan = {
            "direction": "long",
            "entry": 100.0,
            "sl": 95.0,
            "tp": 110.0,
            "rr": 2.0,
            "zone": (98.0, 100.0),
        }
        plan.update(extra)
        return plan

    def test_signal_fills_and_takes_profit(self, tmp_path):
        candles = [
            _mk(SESSION_BASE, o=105, h=106, low=104, c=105),  # approval cycle
            _mk(SESSION_BASE + _M5, o=105, h=105, low=99.5, c=101),  # touch -> fill
            _mk(SESSION_BASE + _M5 * 2, o=101, h=111, low=100.5, c=110.5),  # tp
        ]
        journal = _journal(tmp_path)
        engine = _StubEngine({SESSION_BASE + _M5: self._plan()})
        run = run_backtest(
            "ETHUSD", [], [], candles,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=journal,
            engine=engine,
            require_full_windows=False,
        )
        assert len(run.records) == 1
        signal = run.records[0].signal
        assert signal["status"] == "tp"
        assert signal["filled_at"] is not None
        assert run.records[0].result_r() == 2.0

    def test_tp_and_sl_in_one_candle_count_as_the_stop(self, tmp_path):
        candles = [
            _mk(SESSION_BASE, o=105, h=106, low=104, c=105),
            _mk(SESSION_BASE + _M5, o=105, h=111, low=94, c=100),  # fill+tp+sl
        ]
        journal = _journal(tmp_path)
        engine = _StubEngine({SESSION_BASE + _M5: self._plan()})
        run = run_backtest(
            "ETHUSD", [], [], candles,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=journal,
            engine=engine,
            require_full_windows=False,
        )
        assert run.records[0].signal["status"] == "sl"
        assert run.records[0].result_r() == -1.0

    def test_same_zone_same_session_is_announced_once(self, tmp_path):
        candles = [
            _mk(SESSION_BASE + _M5 * i, o=105, h=106, low=104, c=105)
            for i in range(3)
        ]
        plans = {
            SESSION_BASE + _M5: self._plan(),
            SESSION_BASE + _M5 * 2: self._plan(entry=99.5),  # same zone, new FVG
        }
        run = run_backtest(
            "ETHUSD", [], [], candles,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=_journal(tmp_path),
            engine=_StubEngine(plans),
            require_full_windows=False,
        )
        assert len(run.records) == 1
        assert run.deduped == 1

    def test_a_different_zone_is_a_new_signal(self, tmp_path):
        candles = [
            _mk(SESSION_BASE + _M5 * i, o=105, h=106, low=104, c=105)
            for i in range(3)
        ]
        plans = {
            SESSION_BASE + _M5: self._plan(),
            SESSION_BASE + _M5 * 2: self._plan(zone=(90.0, 92.0), entry=92.0, sl=89.0),
        }
        run = run_backtest(
            "ETHUSD", [], [], candles,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=_journal(tmp_path),
            engine=_StubEngine(plans),
            require_full_windows=False,
        )
        assert len(run.records) == 2
        assert run.deduped == 0


class TestHistoryCache:
    def _candles(self, n=5, start=SESSION_BASE):
        return [_mk(start + _M5 * i, c=100.0 + i) for i in range(n)]

    def test_round_trip_preserves_candles(self, tmp_path):
        path = cache_path(tmp_path, "ETHUSD", "m5")
        candles = self._candles()
        save_cache(path, candles)
        assert load_cache(path) == candles

    def test_corrupt_cache_reads_as_empty(self, tmp_path):
        path = cache_path(tmp_path, "ETHUSD", "m5")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert load_cache(path) == []

    def test_merge_dedupes_by_timestamp_and_sorts(self):
        a = self._candles(3)
        b = self._candles(3, start=SESSION_BASE + _M5 * 2)  # overlaps a[2]
        merged = merge_candles(a, b)
        assert [c.timestamp for c in merged] == sorted({c.timestamp for c in a + b})
        # later series wins the duplicate stamp
        assert merged[2] == b[0]

    @pytest.mark.asyncio
    async def test_covered_range_never_fetches(self, tmp_path, monkeypatch):
        from app.services.smc import history

        candles = self._candles(10)
        save_cache(cache_path(tmp_path, "ETHUSD", "m5"), candles)

        async def _boom(*args, **kwargs):
            raise AssertionError("covered range must be served from cache")

        monkeypatch.setattr(history, "fetch_history", _boom)
        got = await load_history(
            "ETHUSD", "m5",
            candles[1].timestamp, candles[-2].timestamp,
            cache_dir=tmp_path,
        )
        assert got == candles[1:-1]

    @pytest.mark.asyncio
    async def test_only_the_missing_tail_is_fetched(self, tmp_path, monkeypatch):
        from app.services.smc import history

        cached = self._candles(5)
        save_cache(cache_path(tmp_path, "ETHUSD", "m5"), cached)
        tail = self._candles(5, start=cached[-1].timestamp + _M5)
        calls = []

        async def _fake(pair, tf, start, end, api_key=None):
            calls.append((start, end))
            return tail

        monkeypatch.setattr(history, "fetch_history", _fake)
        got = await load_history(
            "ETHUSD", "m5",
            cached[0].timestamp, tail[-1].timestamp,
            cache_dir=tmp_path,
        )
        assert got == cached + tail
        assert calls == [(cached[-1].timestamp, tail[-1].timestamp)]
        # and the cache file now carries the merged series
        assert load_cache(cache_path(tmp_path, "ETHUSD", "m5")) == cached + tail


class TestMetrics:
    def _record(self, status, rr=2.0, result_r=None, warnings=(), when=SESSION_BASE):
        signal = {
            "status": status,
            "rr": rr,
            "result_r": result_r,
            "created_at": when.isoformat(),
            "resolved_at": when.isoformat(),
            "filled_at": when.isoformat() if status not in ("expired", "pending") else None,
            "tier": "regular",
        }
        return SignalRecord(
            signal=signal,
            warnings=list(warnings),
            direction_source="h4",
            session_block="NY",
            fingerprint="fp",
        )

    def test_stats_over_a_mixed_ledger(self):
        base = SESSION_BASE
        records = [
            self._record("tp", rr=2.0, when=base),
            self._record("sl", when=base + _M5),
            self._record("tp1_be", result_r=1.0, when=base + _M5 * 2),
            self._record("tp1_runner", result_r=2.5, when=base + _M5 * 3),
            self._record("expired", when=base + _M5 * 4),
        ]
        stats = compute_stats(records)
        assert (stats.n, stats.wins, stats.losses, stats.scratches) == (5, 2, 1, 1)
        assert stats.expired == 1
        assert stats.total_r == 4.5
        assert stats.winrate == 2 / 3
        assert stats.expectancy_r == 4.5 / 4
        assert stats.profit_factor == 5.5
        assert stats.max_consecutive_losses == 1
        assert stats.max_drawdown_r == 1.0

    def test_plain_signals_map_tp_to_rr_and_sl_to_minus_one(self):
        assert self._record("tp", rr=1.7).result_r() == 1.7
        assert self._record("sl").result_r() == -1.0
        assert self._record("pending").result_r() is None

    def test_warning_autopsy_splits_on_the_engine_labels(self):
        records = [
            self._record("sl", warnings=["price has run 1.2R past the imbalance"]),
            self._record("tp"),
        ]
        autopsy = warning_autopsy(records)
        assert len(autopsy) == 1
        label, with_w, without = autopsy[0]
        assert "5.1" in label
        assert (with_w.n, without.n) == (1, 1)
        assert with_w.losses == 1 and without.wins == 1

    def test_star_miss_autopsy_splits_per_condition(self):
        records = [
            self._record("sl"),  # a star: missed nothing
            self._record("tp"),
            self._record("sl"),
        ]
        records[1].tier_missed = ["pd"]
        records[2].tier_missed = ["pd", "room"]
        autopsy = dict(
            (name, (missing, having))
            for name, missing, having in star_miss_autopsy(records)
        )
        assert set(autopsy) == {"pd", "room"}
        assert autopsy["pd"][0].n == 2 and autopsy["pd"][1].n == 1
        assert autopsy["room"][0].n == 1 and autopsy["room"][1].n == 2

    def test_one_away_from_star_keeps_only_single_miss_setups(self):
        records = [
            self._record("tp"),  # star — not one away
            self._record("tp"),
            self._record("sl"),
        ]
        records[1].tier_missed = ["pd"]
        records[2].tier_missed = ["pd", "room"]  # two away — excluded
        one_away = one_away_from_star(records)
        assert [name for name, _ in one_away] == ["pd"]
        assert one_away[0][1].n == 1
        assert one_away[0][1].wins == 1

    def test_tier_missed_flows_from_the_engine_into_the_record(self, tmp_path):
        # via the loop: a stub plan that misses two star conditions
        candles = [
            _mk(SESSION_BASE + _M5 * i, o=105, h=106, low=104, c=105)
            for i in range(2)
        ]
        plans = {
            SESSION_BASE + _M5: {
                "direction": "long", "entry": 100.0, "sl": 95.0, "tp": 110.0,
                "rr": 2.0, "zone": (98.0, 100.0), "missed": ["pd", "trend"],
            },
        }
        run = run_backtest(
            "ETHUSD", [], [], candles,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=1),
            journal=_journal(tmp_path),
            engine=_StubEngine(plans),
            require_full_windows=False,
        )
        assert run.records[0].tier_missed == ["pd", "trend"]
        assert run.records[0].signal["tier"] == "regular"

    def test_combined_summary_totals_every_pair(self):
        run_a = BacktestRun(
            pair="ETHUSD", profile_key="conservative",
            start=SESSION_BASE, end=SESSION_BASE + timedelta(days=1),
            records=[self._record("tp")],
        )
        run_b = BacktestRun(
            pair="USDJPY", profile_key="conservative",
            start=SESSION_BASE, end=SESSION_BASE + timedelta(days=1),
            records=[self._record("sl")],
        )
        combined = render_combined([run_a, run_b])
        assert "ETHUSD" in combined and "USDJPY" in combined
        assert "TOTAL" in combined
        assert "n=2" in combined.split("TOTAL")[1]

    def test_report_groups_by_month(self):
        run = BacktestRun(
            pair="ETHUSD", profile_key="conservative",
            start=SESSION_BASE, end=SESSION_BASE + timedelta(days=40),
            records=[
                self._record("tp", when=SESSION_BASE),
                self._record("sl", when=SESSION_BASE + timedelta(days=31)),
            ],
        )
        report = render_report(run)
        assert "by month" in report
        assert "2026-07" in report and "2026-08" in report

    def test_report_carries_the_not_simulated_disclaimer(self):
        run = BacktestRun(
            pair="USDJPY",
            profile_key="conservative",
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(days=1),
            records=[self._record("tp")],
        )
        report = render_report(run)
        assert "USDJPY" in report
        assert "NOT simulated" in report
        assert "news blackout" in report

    def test_empty_run_still_renders(self):
        run = BacktestRun(
            pair="ETHUSD",
            profile_key="conservative",
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(days=1),
        )
        stats = compute_stats(run.records)
        assert isinstance(stats, Stats)
        assert stats.winrate is None and stats.expectancy_r is None
        assert "ETHUSD" in render_report(run)


class TestRealEngineSmoke:
    def test_the_production_engine_replays_without_a_scripted_market(self, tmp_path):
        # A quiet drifting market: the engine should find no setup, and the
        # loop should still classify every in-session cycle.
        closes = [3100 + (i % 7) for i in range(60)]
        m5 = make_candles(closes)
        h1 = make_candles(closes[:24], start=SESSION_BASE - timedelta(hours=24), step_minutes=60)
        h4 = make_candles(closes[:12], start=SESSION_BASE - timedelta(hours=48), step_minutes=240)
        journal = _journal(tmp_path)
        run = run_backtest(
            "ETHUSD", h4, h1, m5,
            start=SESSION_BASE,
            end=SESSION_BASE + timedelta(hours=5),
            journal=journal,
            require_full_windows=False,
        )
        assert run.cycles > 0
        assert sum(run.verdicts.values()) == run.cycles
        # every recorded signal (if any) went through the real journal
        for record in run.records:
            assert journal.get(record.signal["id"]) is not None
