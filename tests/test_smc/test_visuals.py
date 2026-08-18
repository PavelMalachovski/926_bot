"""Tests for the visual upgrade pack: DB migration, trade marks, discipline,
live-card events, chart rendering, pretty stats, digest timeline."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

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

# Deliberately relative to the real clock, never a fixed calendar date:
# `journal.stats_text` only reports the last 30 days, so a hardcoded NOW
# silently ages out of that window and the journal reads empty. This file
# failed for exactly that reason once. Every consumer either stamps a signal
# with this instant or passes it straight to `discipline_block`, which
# compares Prague dates on both sides — so any single consistent instant
# works, as long as it stays inside the stats window.
NOW = datetime.now(tz=timezone.utc) - timedelta(days=1)


def _approved_result() -> AnalysisResult:
    result = AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )
    result.session_name = "New York"
    result = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(
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

    def test_old_not_null_take_profit_is_relaxed_and_null_persists(self, tmp_path):
        """Detector mode: a setup with no unswept liquidity ahead has no
        take-profit. Databases created before 2026-08-06 declared the column
        NOT NULL, which SQLite cannot drop in place — the migration must
        rebuild the table and keep every existing row."""
        path = str(tmp_path / "old_notnull.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT)"""
        )
        conn.execute(
            "INSERT INTO signals (id, pair, direction, entry, stop_loss, "
            "take_profit, rr, created_at, status) VALUES "
            "('old1', 'ETHUSD', 'long', 100.0, 95.0, 110.0, 2.0, "
            "'2026-07-16T14:00:00+00:00', 'pending')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        db.signal_upsert(
            {
                "id": "new1",
                "pair": "ETHUSD",
                "direction": "long",
                "entry": 100.0,
                "stop_loss": 95.0,
                "take_profit": None,
                "rr": 0.0,
                "created_at": "2026-07-16T15:00:00+00:00",
                "status": "pending",
            }
        )
        rows = {s["id"]: s for s in db.signals_all()}
        assert rows["old1"]["take_profit"] == 110.0  # untouched by the rebuild
        assert rows["new1"]["take_profit"] is None
        # the id primary key survived the rebuild
        assert [r["pk"] for r in db.conn.execute("PRAGMA table_info(signals)")
                if r["name"] == "id"] == [1]

        # Re-opening must be a no-op: the migration is keyed on the constraint
        # it removes, so a second pass may neither run again nor lose rows.
        again = Database(path)
        assert {s["id"]: s["take_profit"] for s in again.signals_all()} == {
            "old1": 110.0, "new1": None
        }
        assert not any(
            r["name"] == "take_profit" and r["notnull"]
            for r in again.conn.execute("PRAGMA table_info(signals)")
        )
        assert "signals_migrated" not in {
            r["name"] for r in again.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    def test_a_broken_migration_does_not_crash_the_watcher(self, tmp_path):
        """db.py must never take the process down (CLAUDE.md): a bot that
        cannot start sends no alerts at all. A leftover scratch table of a
        foreign shape used to raise OperationalError out of the constructor."""
        path = str(tmp_path / "hostile.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT)"""
        )
        conn.execute(
            "INSERT INTO signals (id, pair, direction, entry, stop_loss, "
            "take_profit, rr, created_at, status) VALUES "
            "('keep', 'ETHUSD', 'long', 100.0, 95.0, 110.0, 2.0, "
            "'2026-07-16T14:00:00+00:00', 'pending')"
        )
        # A scratch table of a completely different shape, holding a row that
        # would also collide on the primary key.
        conn.execute("CREATE TABLE signals_migrated (nonsense TEXT)")
        conn.execute("INSERT INTO signals_migrated VALUES ('keep')")
        conn.commit()
        conn.close()

        db = Database(path)  # must not raise
        # The residue is dropped, so the rebuild completes and the row lives.
        assert [s["id"] for s in db.signals_all()] == ["keep"]
        db.signal_upsert(
            {
                "id": "no_tp",
                "pair": "ETHUSD",
                "direction": "long",
                "entry": 100.0,
                "stop_loss": 95.0,
                "take_profit": None,
                "rr": 0.0,
                "created_at": "2026-07-16T15:00:00+00:00",
                "status": "pending",
            }
        )
        assert {s["id"] for s in db.signals_all()} == {"keep", "no_tp"}

    def test_a_failing_migration_is_logged_not_raised(self, tmp_path):
        """Whatever the failure, the constructor returns a usable Database.

        Injected failure: `signals_migrated` exists as a VIEW, so even
        `DROP TABLE IF EXISTS` raises ("use DROP VIEW to delete view").
        """
        path = str(tmp_path / "failing.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT)"""
        )
        conn.execute(
            "INSERT INTO signals (id, pair, direction, entry, stop_loss, "
            "take_profit, rr, created_at, status) VALUES "
            "('keep', 'ETHUSD', 'long', 100.0, 95.0, 110.0, 2.0, "
            "'2026-07-16T14:00:00+00:00', 'pending')"
        )
        conn.execute("CREATE VIEW signals_migrated AS SELECT 1")
        conn.commit()
        conn.close()

        db = Database(path)  # must not raise
        # The legacy (still NOT NULL) table is intact and usable: only the
        # rare take-profit-less signal is lost, never the whole watcher.
        assert [s["id"] for s in db.signals_all()] == ["keep"]
        assert any(
            r["name"] == "take_profit" and r["notnull"]
            for r in db.conn.execute("PRAGMA table_info(signals)")
        )

    def test_rebuild_reports_columns_it_would_drop(self, tmp_path, monkeypatch):
        """A legacy column outside SIGNAL_COLUMNS is dropped by the rebuild —
        that must be loud, so a future mismatch fails visibly."""
        path = str(tmp_path / "extra_column.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE signals (
                id TEXT PRIMARY KEY, pair TEXT NOT NULL, direction TEXT NOT NULL,
                entry REAL NOT NULL, stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL, rr REAL NOT NULL, session TEXT,
                created_at TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL,
                filled_at TEXT, resolved_at TEXT, checked_until TEXT,
                owner_note TEXT)"""
        )
        conn.commit()
        conn.close()

        import app.services.smc.db as db_mod

        errors = []

        class _Spy:
            def error(self, event, **kw):
                errors.append((event, kw))

            def info(self, event, **kw):
                pass

        monkeypatch.setattr(db_mod, "logger", _Spy())
        db = Database(path)
        assert any("owner_note" in (kw.get("columns") or []) for _, kw in errors)
        # ...and the rebuild still went through
        assert not any(
            r["name"] == "take_profit" and r["notnull"]
            for r in db.conn.execute("PRAGMA table_info(signals)")
        )

    def test_fresh_db_accepts_a_null_take_profit(self, tmp_path):
        db = Database(str(tmp_path / "fresh.db"))
        db.signal_upsert(
            {
                "id": "s1",
                "pair": "ETHUSD",
                "direction": "long",
                "entry": 100.0,
                "stop_loss": 95.0,
                "take_profit": None,
                "rr": 0.0,
                "created_at": "2026-07-16T14:00:00+00:00",
                "status": "pending",
            }
        )
        assert db.signals_all()[0]["take_profit"] is None


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
    def test_update_pair_reports_fill_and_tp1_runner(self, tmp_path):
        """Phase 2 sniper redesign: every setup the real engine approves now
        carries tp1/runner_tp (engine.py, Task 2), so a freshly recorded
        signal always runs the hybrid partial-close lifecycle, not the plain
        tp/sl one. entry=3139.5, sl=3128.0 -> risk=11.5, tp1=3162.5 (2R),
        runner_tp=3174.0 (3R)."""
        journal = SignalJournal(Database(str(tmp_path / "j.db")))
        result = _approved_result()
        signal = journal.record(result)
        assert signal["tp1"] == 3162.5 and signal["runner_tp"] == 3174.0
        start = datetime(2026, 7, 6, 15, 45, tzinfo=timezone.utc)
        candles = [
            candle(3142, 3143, 3139.0, 3141, start=start, index=0),  # fills 3139.5
            candle(3141, 3163, 3140, 3162, start=start, index=1),  # tags TP1 3162.5
            candle(3162, 3180, 3170, 3178, start=start, index=2),  # runner 3174.0
        ]
        events = journal.update_pair("ETHUSD", candles)
        assert [e for _, e in events] == ["filled", "tp1_runner"]
        assert signal["status"] == "tp1_runner"
        assert signal["result_r"] == pytest.approx(2.5)

    def test_signal_without_a_take_profit_still_fills_and_stops(self, tmp_path):
        """Detector mode: a setup with no unswept liquidity ahead is recorded
        and tracked; only "TP hit" is impossible for it."""
        journal = SignalJournal(Database(str(tmp_path / "j.db")))
        result = _approved_result()
        result.setup.take_profit = None
        result.setup.rr = 0.0
        signal = journal.record(result)
        assert signal["take_profit"] is None
        start = datetime(2026, 7, 6, 15, 45, tzinfo=timezone.utc)
        candles = [
            candle(3142, 3143, 3139.0, 3141, start=start, index=0),  # fills 3139.5
            candle(3141, 3225, 3120, 3125, start=start, index=1),  # SL 3128.0
        ]
        events = journal.update_pair("ETHUSD", candles)
        assert [e for _, e in events] == ["filled", "sl"]
        assert signal["status"] == "sl"

    def test_signal_without_a_take_profit_never_resolves_as_tp(self, tmp_path):
        journal = SignalJournal(Database(str(tmp_path / "j.db")))
        result = _approved_result()
        result.setup.take_profit = None
        signal = journal.record(result)
        start = datetime(2026, 7, 6, 15, 45, tzinfo=timezone.utc)
        candles = [
            candle(3142, 3143, 3139.0, 3141, start=start, index=0),  # fills
            candle(3141, 3500, 3140, 3480, start=start, index=1),  # miles up
        ]
        events = journal.update_pair("ETHUSD", candles)
        # It fills, but a 350-point rally cannot resolve a signal that has no
        # objective. (The fixture's created_at is older than OPEN_TIMEOUT, so
        # the open position is then swept up by the 5-day safety valve — what
        # matters here is that "tp" is never reached.)
        assert "tp" not in [e for _, e in events]
        assert signal["status"] != "tp"
        assert signal["filled_at"] is not None

    def test_live_card_does_not_promise_to_track_a_tp_that_does_not_exist(self):
        """The card footer for an open position: with no objective recorded,
        `evaluate_signal` can only ever resolve it as SL or timeout, so
        "tracking TP/SL" would be a promise the bot cannot keep."""
        from smc_watcher import _card_footer

        signal = {
            "status": "open", "entry": 3139.5, "rr": 2.0,
            "filled_at": None, "resolved_at": None, "take_profit": 3219.0,
        }
        assert "tracking TP/SL" in _card_footer(signal)

        signal["take_profit"] = None
        footer = _card_footer(signal)
        assert "TP/SL" not in footer
        assert "tracking SL (no objective recorded)" in footer


class TestChart:
    def test_renders_png(self):
        from app.services.smc.chart import render_setup_chart

        png = render_setup_chart(_approved_result())
        assert png is not None and png[:4] == b"\x89PNG"

    def test_renders_png_without_a_take_profit(self):
        """Detector mode: no unswept liquidity ahead -> take_profit is None.
        The TP line and its edge annotation are skipped, the chart still
        renders (rendering must never block an alert)."""
        from app.services.smc.chart import render_setup_chart

        result = _approved_result()
        result.setup.take_profit = None
        result.setup.target = None
        result.setup.rr = 0.0
        png = render_setup_chart(result)
        assert png is not None and png[:4] == b"\x89PNG"

    def test_returns_none_without_candles(self):
        from app.services.smc.chart import render_setup_chart

        result = _approved_result()
        result.m5_candles = None
        assert render_setup_chart(result) is None

    def test_far_away_take_profit_does_not_blow_out_the_ylim(self, monkeypatch):
        """A liquidity TP can sit an H4 pool away from price (hundreds of
        points); the y-axis must stay clamped to the candle range instead of
        autoscaling out to include it (Important finding 1)."""
        import app.services.smc.chart as chart_mod

        result = _approved_result()
        candles = result.m5_candles[-chart_mod.CHART_CANDLES:]
        candle_hi = max(c.high for c in candles)
        candle_lo = min(c.low for c in candles)
        candle_span = candle_hi - candle_lo

        # Push the take-profit far outside the candle range, like a distant
        # H4 liquidity pool the old fixed-RR rule could never produce.
        result.setup.take_profit = candle_hi + 200.0

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            real_set_ylim = ax.set_ylim

            def spy_set_ylim(*a, **kw):
                out = real_set_ylim(*a, **kw)
                captured["ylim"] = ax.get_ylim()
                return out

            ax.set_ylim = spy_set_ylim
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)

        png = chart_mod.render_setup_chart(result)

        assert png is not None and png[:4] == b"\x89PNG"
        assert "ylim" in captured
        ylo, yhi = captured["ylim"]
        # The window still brackets the candle range tightly — nowhere near
        # the take-profit 200 points above it.
        assert ylo <= candle_lo
        assert yhi < candle_hi + candle_span
        assert yhi - ylo < candle_span * 1.5

    def test_far_take_profit_draws_edge_annotation_not_axhline(self):
        """A level outside the y-window gets a fixed edge annotation, never
        an autoscaling axhline (the mechanism behind the blowout): no Line2D
        is added at the far price, and the axis limits are untouched."""
        import matplotlib.pyplot as plt

        from app.services.smc.chart import _level, _price_ylim

        candles = _approved_result().m5_candles[-50:]
        ylim = _price_ylim(candles, (3140.0, 3130.0))
        fig, ax = plt.subplots()
        try:
            ax.set_ylim(*ylim)
            far_price = ylim[1] + 500.0
            _level(ax, far_price, "#089981", "TP 9999.00", 10, y_bounds=ylim)
            assert ax.get_ylim() == ylim  # unaffected by drawing the level
            assert ax.get_lines() == []  # no axhline drawn for the far price
            assert len(ax.texts) == 1  # the edge annotation itself
            assert "↑" in ax.texts[0].get_text()
        finally:
            plt.close(fig)

    def test_draws_the_order_block_box_when_present(self, monkeypatch):
        """§2.4's "5m OB box": `setup.order_block` (the deeper M5 limit
        option, engine.py) gets its own shaded rectangle, in the style of the
        existing FVG box, spanning its own bottom/top."""
        import app.services.smc.chart as chart_mod

        result = _approved_result()
        ob = result.setup.order_block
        assert ob is not None  # fixture sanity: this setup has one

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            captured["ax"] = ax
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_setup_chart(result)

        assert png is not None and png[:4] == b"\x89PNG"
        ob_boxes = [
            p for p in captured["ax"].patches
            if isinstance(p, chart_mod.Rectangle)
            and p.get_y() == pytest.approx(ob.bottom)
            and p.get_height() == pytest.approx(ob.top - ob.bottom)
        ]
        assert len(ob_boxes) == 1

    def test_skips_the_order_block_box_when_absent(self):
        """No order block found -> the chart still renders (rendering must
        never block an alert), just without that rectangle."""
        from app.services.smc.chart import render_setup_chart

        result = _approved_result()
        result.setup.order_block = None
        png = render_setup_chart(result)
        assert png is not None and png[:4] == b"\x89PNG"


class TestZoneKindOnCharts:
    def test_setup_chart_renders_with_an_fvg_zone(self):
        """A zone of kind FVG renders exactly like an OB one — the kind is a
        label, not a geometry change."""
        from app.services.smc.chart import render_setup_chart

        result_ob = _approved_result()
        result_fvg = _approved_result()
        assert result_fvg.h1_zone.kind == "OB"  # fixture default, sanity check
        result_fvg.h1_zone.kind = "FVG"

        png_ob = render_setup_chart(result_ob)
        png_fvg = render_setup_chart(result_fvg)

        assert png_fvg is not None and png_fvg[:4] == b"\x89PNG"
        assert len(png_fvg) > 1000
        # The H1 zone band is colored by is_demand, never labelled by kind on
        # this chart — changing only the kind must not move a single pixel.
        assert png_fvg == png_ob

    def test_plan_chart_renders_with_both_zone_kinds(self, monkeypatch):
        """Two scenarios, one OB and one FVG, both draw — and the entry
        label on each names its own kind."""
        import app.services.smc.chart as chart_mod
        from app.services.smc.models import Direction, Trend
        from app.services.smc.plan import PairPlan, PlanScenario

        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        plan = PairPlan(
            pair="ETHUSD",
            price=3135.0,
            price_decimals=2,
            h4_trend=Trend.UP,
            scenarios=[
                PlanScenario(
                    direction=Direction.LONG, entry=3138.0, stop_loss=3128.0,
                    take_profit=3170.0, rr=2.0, zone_bottom=3131.0,
                    zone_top=3138.0, speculative=False, kind="OB",
                ),
                PlanScenario(
                    direction=Direction.SHORT, entry=3140.0, stop_loss=3150.0,
                    take_profit=3100.0, rr=2.0, zone_bottom=3140.0,
                    zone_top=3145.0, speculative=False, kind="FVG",
                ),
            ],
        )

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            captured["ax"] = ax
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)

        png = chart_mod.render_plan_chart(plan, h1)

        assert png is not None and png[:4] == b"\x89PNG"
        assert len(png) > 1000
        texts = [t.get_text() for t in captured["ax"].texts]
        assert any("Entry 3138.00 (OB)" in t for t in texts)
        assert any("Entry 3140.00 (FVG)" in t for t in texts)


def _zone_bands(ax):
    """The axhspan-drawn zone-band rectangles on a plan chart — full-width
    in axes-fraction x (x=0, width=1) — as opposed to the per-candle body
    rectangles from `_draw_candles`, which sit at data-coordinate x and are
    narrower than the full axes width."""
    import matplotlib.patches as mpatches

    return [
        p for p in ax.patches
        if isinstance(p, mpatches.Rectangle)
        and p.get_x() == 0 and p.get_width() == 1
    ]


class TestPlanChartRunnerUpZone:
    """spec 2026-08-16 §2.4: both zone candidates drawn and labelled, OB and
    FVG in distinct colours."""

    def _plan_with_runner_up(self, runner_up_kind="FVG"):
        from app.services.smc.models import Direction, Trend, Zone
        from app.services.smc.plan import PairPlan, PlanScenario

        ts = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
        runner_up = Zone(
            bottom=3120.0, top=3129.0, is_demand=True, pivot_index=0,
            timestamp=ts, kind=runner_up_kind,
        )
        return PairPlan(
            pair="ETHUSD",
            price=3160.0,
            price_decimals=2,
            h4_trend=Trend.UP,
            scenarios=[
                PlanScenario(
                    direction=Direction.LONG, entry=3138.0, stop_loss=3128.0,
                    take_profit=3170.0, rr=2.0, zone_bottom=3131.0,
                    zone_top=3138.0, speculative=False, kind="OB",
                    runner_up=runner_up,
                ),
            ],
        )

    def _render_captured(self, plan, monkeypatch):
        import app.services.smc.chart as chart_mod

        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            captured["ax"] = ax
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_plan_chart(plan, h1)
        return png, captured

    def test_both_bands_are_drawn(self, monkeypatch):
        """A scenario with a runner-up draws two zone bands, not one — the
        gap this feature closes: the branch used to be text-only."""
        plan = self._plan_with_runner_up()
        png, captured = self._render_captured(plan, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        bands = _zone_bands(captured["ax"])
        assert len(bands) == 2
        bounds = sorted((round(b.get_y(), 2), round(b.get_y() + b.get_height(), 2)) for b in bands)
        assert bounds == [(3120.0, 3129.0), (3131.0, 3138.0)]

    def test_ob_and_fvg_bands_use_distinct_colours(self, monkeypatch):
        """OB -> OB_COLOR (amber, shared with the setup chart's order-block
        box); FVG -> the new violet FVG_COLOR. Never the same colour."""
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod

        plan = self._plan_with_runner_up(runner_up_kind="FVG")
        _, captured = self._render_captured(plan, monkeypatch)
        bands = _zone_bands(captured["ax"])
        assert len(bands) == 2

        winner = next(b for b in bands if round(b.get_y(), 2) == 3131.0)
        runner = next(b for b in bands if round(b.get_y(), 2) == 3120.0)

        ob_rgb = mcolors.to_rgb(chart_mod.OB_COLOR)
        fvg_rgb = mcolors.to_rgb(chart_mod.FVG_COLOR)
        assert mcolors.to_rgb(winner.get_facecolor()) == pytest.approx(ob_rgb)
        assert mcolors.to_rgb(runner.get_facecolor()) == pytest.approx(fvg_rgb)
        assert ob_rgb != fvg_rgb

    def test_winner_is_more_opaque_than_the_runner_up(self, monkeypatch):
        """The winner is what the plan is actually built on and should read
        first; the runner-up is dimmer, so the eye lands on the winner."""
        import app.services.smc.chart as chart_mod

        plan = self._plan_with_runner_up()
        _, captured = self._render_captured(plan, monkeypatch)
        bands = _zone_bands(captured["ax"])

        winner = next(b for b in bands if round(b.get_y(), 2) == 3131.0)
        runner = next(b for b in bands if round(b.get_y(), 2) == 3120.0)

        assert winner.get_alpha() == pytest.approx(chart_mod.WINNER_ZONE_ALPHA)
        assert runner.get_alpha() == pytest.approx(chart_mod.RUNNER_UP_ZONE_ALPHA)
        assert winner.get_alpha() > runner.get_alpha()

    def test_runner_up_band_has_a_dashed_edge_the_winner_does_not(self, monkeypatch):
        plan = self._plan_with_runner_up()
        _, captured = self._render_captured(plan, monkeypatch)
        bands = _zone_bands(captured["ax"])

        winner = next(b for b in bands if round(b.get_y(), 2) == 3131.0)
        runner = next(b for b in bands if round(b.get_y(), 2) == 3120.0)

        assert runner.get_linestyle() in ("--", "dashed")
        assert winner.get_linestyle() not in ("--", "dashed")

    def test_bands_are_labelled_by_direction_and_kind(self, monkeypatch):
        plan = self._plan_with_runner_up(runner_up_kind="FVG")
        _, captured = self._render_captured(plan, monkeypatch)
        texts = captured["ax"].texts
        by_text = {t.get_text(): t for t in texts}

        assert "Demand OB" in by_text
        assert "Demand FVG (alt)" in by_text
        # left edge of the plot, not on top of the candles (which start x=0)
        assert by_text["Demand OB"].get_position()[0] < 0
        assert by_text["Demand FVG (alt)"].get_position()[0] < 0
        # vertically centred in its own band
        assert by_text["Demand OB"].get_position()[1] == pytest.approx(
            (3131.0 + 3138.0) / 2
        )
        assert by_text["Demand FVG (alt)"].get_position()[1] == pytest.approx(
            (3120.0 + 3129.0) / 2
        )

    def test_no_runner_up_draws_one_band_and_no_alt_label(self, monkeypatch):
        """A scenario with no runner-up (the default, and every scenario
        before this feature) must render exactly as before: one band, no
        "(alt)" label, no dashed edge anywhere."""
        from app.services.smc.models import Direction, Trend
        from app.services.smc.plan import PairPlan, PlanScenario

        plan = PairPlan(
            pair="ETHUSD",
            price=3160.0,
            price_decimals=2,
            h4_trend=Trend.UP,
            scenarios=[
                PlanScenario(
                    direction=Direction.LONG, entry=3138.0, stop_loss=3128.0,
                    take_profit=3170.0, rr=2.0, zone_bottom=3131.0,
                    zone_top=3138.0, speculative=False, kind="OB",
                ),
            ],
        )
        png, captured = self._render_captured(plan, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        bands = _zone_bands(captured["ax"])
        assert len(bands) == 1
        assert bands[0].get_linestyle() not in ("--", "dashed")
        texts = [t.get_text() for t in captured["ax"].texts]
        assert not any("(alt)" in t for t in texts)

    def test_ylim_brackets_a_runner_up_outside_the_candle_range(self, monkeypatch):
        """A runner-up zone can sit outside the visible H1 candles; the
        y-axis must extend to include it or the band clips invisibly (the
        same failure mode the far-away take-profit fix guards against on
        the setup chart)."""
        import app.services.smc.chart as chart_mod
        from app.services.smc.models import Direction, Trend, Zone
        from app.services.smc.plan import PairPlan, PlanScenario

        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        candle_lo = min(c.low for c in h1)
        far_below = candle_lo - 500.0
        ts = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
        runner_up = Zone(
            bottom=far_below, top=far_below + 5.0, is_demand=True,
            pivot_index=0, timestamp=ts, kind="FVG",
        )
        plan = PairPlan(
            pair="ETHUSD",
            price=3160.0,
            price_decimals=2,
            h4_trend=Trend.UP,
            scenarios=[
                PlanScenario(
                    direction=Direction.LONG, entry=3138.0, stop_loss=3128.0,
                    take_profit=3170.0, rr=2.0, zone_bottom=3131.0,
                    zone_top=3138.0, speculative=False, kind="OB",
                    runner_up=runner_up,
                ),
            ],
        )

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            real_set_ylim = ax.set_ylim

            def spy_set_ylim(*a, **kw):
                out = real_set_ylim(*a, **kw)
                captured["ylim"] = ax.get_ylim()
                return out

            ax.set_ylim = spy_set_ylim
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_plan_chart(plan, h1)

        assert png is not None and png[:4] == b"\x89PNG"
        assert "ylim" in captured
        ylo, _ = captured["ylim"]
        assert ylo <= runner_up.bottom

    def test_ylim_ignores_a_runner_up_that_was_never_attached(self, monkeypatch):
        """Regression guard for the opposite mistake: reading `runner_up`
        unconditionally would crash on the (default, common) None case."""
        plan = self._plan_with_runner_up()
        plan.scenarios[0].runner_up = None
        png, captured = self._render_captured(plan, monkeypatch)
        assert png is not None and png[:4] == b"\x89PNG"


def _range(top=3170.0, bottom=3120.0):
    from app.services.smc.range import Range

    ts = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    return Range(
        top=top, bottom=bottom, touches_top=2, touches_bottom=2, broken=False,
        top_at=ts, bottom_at=ts, swept_top=False, swept_bottom=False,
        printed_at=ts,
    )


class TestSetupChartRangeBoundaries:
    """spec 2026-08-16 §3.4 (owner: "отметить этот боковик на графике
    чёрным пунктиром"): both range boundaries as black-ish dashed lines
    labelled RANGE HIGH / RANGE LOW. Gated on `market_range` being set
    alone — per its docstring (models.AnalysisResult.market_range) and
    task-4-brief.md, the field being set is enough for drawing the
    boundaries themselves; it does not require `direction_source ==
    "range"` (that stricter condition is for anything that renders the
    whole SETUP as a range trade, which this is not)."""

    def _render_captured(self, result, monkeypatch):
        import app.services.smc.chart as chart_mod

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            captured["ax"] = ax
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_setup_chart(result)
        return png, captured

    def test_draws_both_boundaries_as_dashed_lines(self, monkeypatch):
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod

        result = _approved_result()
        result.market_range = _range(top=3170.0, bottom=3120.0)

        png, captured = self._render_captured(result, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        ax = captured["ax"]
        range_rgb = mcolors.to_rgb(chart_mod.RANGE_COLOR)
        boundary_lines = [
            ln for ln in ax.get_lines()
            if mcolors.to_rgb(ln.get_color()) == pytest.approx(range_rgb)
        ]
        assert len(boundary_lines) == 2
        ys = sorted(round(ln.get_ydata()[0], 2) for ln in boundary_lines)
        assert ys == [3120.0, 3170.0]
        for ln in boundary_lines:
            assert ln.get_linestyle() in ("--", "dashed")

        texts = [t.get_text().strip() for t in ax.texts]
        assert "RANGE HIGH" in texts
        assert "RANGE LOW" in texts

    def test_no_range_draws_no_boundary_lines(self, monkeypatch):
        """A chart with no range must render exactly as it does today."""
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod

        result = _approved_result()
        assert result.market_range is None  # fixture sanity

        png, captured = self._render_captured(result, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        ax = captured["ax"]
        range_rgb = mcolors.to_rgb(chart_mod.RANGE_COLOR)
        assert not any(
            mcolors.to_rgb(ln.get_color()) == pytest.approx(range_rgb)
            for ln in ax.get_lines()
        )
        texts = [t.get_text() for t in ax.texts]
        assert not any("RANGE" in t for t in texts)

    def test_far_range_boundary_extends_the_ylim(self, monkeypatch):
        """A boundary can sit outside the visible M5 candle range; ylim
        must extend to include it or the line clips invisibly — the same
        fix the far-away take-profit test guards on this chart."""
        import app.services.smc.chart as chart_mod

        result = _approved_result()
        candles = result.m5_candles[-chart_mod.CHART_CANDLES:]
        candle_hi = max(c.high for c in candles)
        far_top = candle_hi + 200.0
        result.market_range = _range(top=far_top, bottom=3120.0)

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            real_set_ylim = ax.set_ylim

            def spy_set_ylim(*a, **kw):
                out = real_set_ylim(*a, **kw)
                captured["ylim"] = ax.get_ylim()
                return out

            ax.set_ylim = spy_set_ylim
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_setup_chart(result)

        assert png is not None and png[:4] == b"\x89PNG"
        assert "ylim" in captured
        _, yhi = captured["ylim"]
        assert yhi >= far_top


class TestPlanChartRangeBoundaries:
    """spec 2026-08-16 §3.4: the H1 plan chart draws the same boundary
    lines whenever the plan carries RANGE scenarios (Task 3, D11/D12)."""

    @staticmethod
    def _range_plan(top=3170.0, bottom=3120.0, sides=("SHORT", "LONG")):
        from app.services.smc.models import Direction, Trend
        from app.services.smc.plan import PairPlan, PlanScenario

        scenarios = []
        if "SHORT" in sides:
            scenarios.append(PlanScenario(
                direction=Direction.SHORT, entry=top, stop_loss=top + 2.0,
                take_profit=bottom + 2.0, rr=2.0, zone_bottom=top - 1.0,
                zone_top=top, speculative=False, kind="RANGE",
            ))
        if "LONG" in sides:
            scenarios.append(PlanScenario(
                direction=Direction.LONG, entry=bottom, stop_loss=bottom - 2.0,
                take_profit=top - 2.0, rr=2.0, zone_bottom=bottom,
                zone_top=bottom + 1.0, speculative=False, kind="RANGE",
            ))
        return PairPlan(
            pair="ETHUSD", price=3145.0, price_decimals=2,
            h4_trend=Trend.FLAT, scenarios=scenarios,
        )

    def _render_captured(self, plan, monkeypatch, h1=None):
        import app.services.smc.chart as chart_mod

        h1 = h1 if h1 is not None else make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            captured["ax"] = ax
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_plan_chart(plan, h1)
        return png, captured

    def test_draws_both_boundaries_as_dashed_lines(self, monkeypatch):
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod

        plan = self._range_plan()
        png, captured = self._render_captured(plan, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        ax = captured["ax"]
        range_rgb = mcolors.to_rgb(chart_mod.RANGE_COLOR)
        boundary_lines = [
            ln for ln in ax.get_lines()
            if mcolors.to_rgb(ln.get_color()) == pytest.approx(range_rgb)
        ]
        assert len(boundary_lines) == 2
        ys = sorted(round(ln.get_ydata()[0], 2) for ln in boundary_lines)
        assert ys == [3120.0, 3170.0]
        for ln in boundary_lines:
            assert ln.get_linestyle() in ("--", "dashed")
        texts = [t.get_text().strip() for t in ax.texts]
        assert "RANGE HIGH" in texts
        assert "RANGE LOW" in texts

    def test_the_band_label_names_the_boundary_not_a_supply_zone(
        self, monkeypatch
    ):
        """Vocabulary fix (review 2026-08-18): the shaded band under a RANGE
        scenario used to be labelled "Supply RANGE" / "Demand RANGE" — a
        boundary is neither."""
        plan = self._range_plan()
        _, captured = self._render_captured(plan, monkeypatch)
        texts = [t.get_text().strip() for t in captured["ax"].texts]
        assert "Supply RANGE" not in texts
        assert "Demand RANGE" not in texts
        # One band label per side, plus the boundary level labels.
        assert texts.count("RANGE HIGH") == 2
        assert texts.count("RANGE LOW") == 2

    def test_only_one_boundary_valid_draws_only_that_line(self, monkeypatch):
        """Only the side present in the plan is drawn. In practice both
        stand or fall together — `plan._range_scenario` gives the two sides
        identical RR by construction (`box height - sl_buffer` of reward
        over `sl_buffer` of risk), so the min_rr filter drops both or
        neither — but the drawing code does not assume it."""
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod

        plan = self._range_plan(sides=("SHORT",))
        png, captured = self._render_captured(plan, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        ax = captured["ax"]
        range_rgb = mcolors.to_rgb(chart_mod.RANGE_COLOR)
        boundary_lines = [
            ln for ln in ax.get_lines()
            if mcolors.to_rgb(ln.get_color()) == pytest.approx(range_rgb)
        ]
        assert len(boundary_lines) == 1
        texts = [t.get_text().strip() for t in ax.texts]
        assert "RANGE HIGH" in texts
        assert "RANGE LOW" not in texts

    def test_no_range_scenarios_draws_no_boundary_lines(self, monkeypatch):
        """A plan chart with no RANGE scenario must render exactly as it
        did before this feature."""
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod
        from app.services.smc.models import Direction, Trend
        from app.services.smc.plan import PairPlan, PlanScenario

        plan = PairPlan(
            pair="ETHUSD", price=3145.0, price_decimals=2, h4_trend=Trend.UP,
            scenarios=[PlanScenario(
                direction=Direction.LONG, entry=3138.0, stop_loss=3128.0,
                take_profit=3170.0, rr=2.0, zone_bottom=3131.0,
                zone_top=3138.0, speculative=False, kind="OB",
            )],
        )
        png, captured = self._render_captured(plan, monkeypatch)

        assert png is not None and png[:4] == b"\x89PNG"
        ax = captured["ax"]
        range_rgb = mcolors.to_rgb(chart_mod.RANGE_COLOR)
        assert not any(
            mcolors.to_rgb(ln.get_color()) == pytest.approx(range_rgb)
            for ln in ax.get_lines()
        )
        texts = [t.get_text() for t in ax.texts]
        assert not any("RANGE" in t for t in texts)

    def test_range_zone_band_is_not_coloured_as_fvg(self, monkeypatch):
        """Carried finding: `_zone_kind_color` used to fall through to
        FVG_COLOR for any non-OB kind, so a RANGE boundary band drew as an
        imbalance. It must use its own colour instead."""
        import matplotlib.colors as mcolors

        import app.services.smc.chart as chart_mod

        plan = self._range_plan(sides=("SHORT",))
        _, captured = self._render_captured(plan, monkeypatch)
        bands = _zone_bands(captured["ax"])
        assert len(bands) == 1
        fvg_rgb = mcolors.to_rgb(chart_mod.FVG_COLOR)
        range_rgb = mcolors.to_rgb(chart_mod.RANGE_COLOR)
        band_rgb = mcolors.to_rgb(bands[0].get_facecolor())
        assert band_rgb != pytest.approx(fvg_rgb)
        assert band_rgb == pytest.approx(range_rgb)

    def test_far_range_boundary_extends_the_ylim(self, monkeypatch):
        """A boundary can sit outside the visible H1 candle range; ylim
        must extend to include it — same fix as the runner-up zone
        (Delivery 2)."""
        import app.services.smc.chart as chart_mod

        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        candle_hi = max(c.high for c in h1[-120:])
        far_top = candle_hi + 500.0
        plan = self._range_plan(top=far_top, bottom=3120.0)

        captured = {}
        real_subplots = chart_mod.plt.subplots

        def spy_subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            real_set_ylim = ax.set_ylim

            def spy_set_ylim(*a, **kw):
                out = real_set_ylim(*a, **kw)
                captured["ylim"] = ax.get_ylim()
                return out

            ax.set_ylim = spy_set_ylim
            return fig, ax

        monkeypatch.setattr(chart_mod.plt, "subplots", spy_subplots)
        png = chart_mod.render_plan_chart(plan, h1)

        assert png is not None and png[:4] == b"\x89PNG"
        assert "ylim" in captured
        _, yhi = captured["ylim"]
        assert yhi >= far_top


class TestChartOffTheEventLoop:
    """Review-hardening Task 6: matplotlib renders (~seconds of CPU for a
    2200x660 PNG) used to run synchronously inside the coroutine — during a
    render the polling loop stalled. Both call sites now go through
    `asyncio.to_thread`; the failure-isolation contract (a bad render must
    never block or break the alert/plan) stays intact either way."""

    @staticmethod
    def _watcher():
        from smc_watcher import Watcher

        w = Watcher.__new__(Watcher)

        class _FakeNotifier:
            def __init__(self):
                self.photos = []
                self.sent = []

            async def send_photo(self, photo, reply_to=None, caption=None):
                self.photos.append(photo)
                return 1

            async def send(self, text, reply_markup=None, disable_notification=False):
                self.sent.append(text)
                return 1

        w.notifier = _FakeNotifier()
        return w

    def test_send_chart_runs_the_render_off_thread_and_survives_a_failure(
        self, monkeypatch
    ):
        import app.services.smc.chart as chart_mod

        calls = []

        def failing_render(result):
            calls.append(result.symbol)
            raise RuntimeError("boom")

        monkeypatch.setattr(chart_mod, "render_setup_chart", failing_render)
        w = self._watcher()
        result = _approved_result()

        # Must not raise (chart rendering must never block/break the alert)
        # and must still reach the (possibly monkeypatched) render function
        # via asyncio.to_thread rather than a direct in-loop call.
        asyncio.run(w._send_chart(result, reply_to=1))

        assert calls == ["ETHUSD"]
        assert w.notifier.photos == []  # failed render -> no photo sent

    def test_send_chart_sends_the_photo_on_success(self, monkeypatch):
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(
            chart_mod, "render_setup_chart", lambda result: b"\x89PNGfake"
        )
        w = self._watcher()
        asyncio.run(w._send_chart(_approved_result(), reply_to=7))

        assert w.notifier.photos == [b"\x89PNGfake"]

    def test_send_chart_offloads_via_asyncio_to_thread(self, monkeypatch):
        """Pins the literal contract: the render call goes through
        `asyncio.to_thread`, not a direct synchronous call on the event
        loop."""
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(chart_mod, "render_setup_chart", lambda result: b"PNG")
        calls = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *a, **kw):
            calls.append(fn)
            return await real_to_thread(fn, *a, **kw)

        monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
        w = self._watcher()
        asyncio.run(w._send_chart(_approved_result(), reply_to=1))

        assert calls == [chart_mod.render_setup_chart]

    def test_deliver_plan_runs_the_render_off_thread_and_survives_a_failure(
        self, monkeypatch
    ):
        import app.services.smc.chart as chart_mod
        from app.services.smc.plan import PairPlan
        from app.services.smc.planbook import PlanEntry
        from app.services.smc.models import Trend

        calls = []

        def failing_render(plan, h1):
            calls.append(plan.pair)
            raise RuntimeError("boom")

        monkeypatch.setattr(chart_mod, "render_plan_chart", failing_render)
        w = self._watcher()
        plan = PairPlan(
            pair="ETHUSD", price=100.0, price_decimals=2, h4_trend=Trend.FLAT,
            market_closed=True,  # skips _live_status, keeps the fixture minimal
        )
        entry = PlanEntry(plan=plan, data={"h4": [], "h1": [], "m5": []}, as_of="12:00")

        asyncio.run(w._deliver_plan("ETHUSD", entry))

        assert calls == ["ETHUSD"]
        assert w.notifier.photos == []  # failed render -> no photo sent
        assert len(w.notifier.sent) == 1  # the plan text still went out

    def test_deliver_plan_offloads_via_asyncio_to_thread(self, monkeypatch):
        import app.services.smc.chart as chart_mod
        from app.services.smc.plan import PairPlan
        from app.services.smc.planbook import PlanEntry
        from app.services.smc.models import Trend

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda plan, h1: b"PNG")
        calls = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *a, **kw):
            calls.append(fn)
            return await real_to_thread(fn, *a, **kw)

        monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
        w = self._watcher()
        plan = PairPlan(
            pair="ETHUSD", price=100.0, price_decimals=2, h4_trend=Trend.FLAT,
            market_closed=True,
        )
        entry = PlanEntry(plan=plan, data={"h4": [], "h1": [], "m5": []}, as_of="12:00")

        asyncio.run(w._deliver_plan("ETHUSD", entry))

        assert calls == [chart_mod.render_plan_chart]


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
        assert "New York 14:00–18:30" in text
        assert "🔴 14:30 CPI m/m (USD) → ETHUSD" in text
        assert "⛔ no entries 13:30–14:45" in text
