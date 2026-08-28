"""Demo autopilot (owner decisions D18-D21, 2026-08-28): paper execution.

Three layers under test:

1. `evaluate_order` — the pure paper fill/exit engine. It must mirror
   `journal.evaluate_signal`'s ordering (SL beats TP inside a candle, the
   TP1 candle never judges BE/runner, BE beats the runner) while adding the
   cost model: strict trade-through limit fills, maker/taker fees, adverse
   slippage on stop-market exits, GTD expiry that a late candle cannot
   fill through, and a timeout that CLOSES at market instead of scoring 0.
2. `Autopilot` — placement sizing off paper equity and the D19 hard gates
   (day-stop on realized stops, weekly kill, max_open, tier filter).
3. Watcher wiring — an order is placed when a signal is recorded, survives
   a failed alert send, and advances on the same candles the journal reads.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.autopilot import (
    AutoConfig,
    Autopilot,
    evaluate_order,
)
from app.services.smc.db import Database
from app.services.smc.journal import OPEN_TIMEOUT, SignalJournal
from app.services.smc.models import (
    AnalysisResult,
    Direction,
    FVG,
    TradeSetup,
    Verdict,
)
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import SESSION_BASE, candle

NOW = SESSION_BASE + timedelta(hours=2)

# Frictionless config: lifecycle assertions in round numbers.
NOFEE = AutoConfig(maker_fee=0.0, taker_fee=0.0, slippage=0.0)
# Default cost model: maker 0.02%, taker 0.05%, slippage 0.02% (fractions).
COSTS = AutoConfig()


def _order(
    *,
    direction="long",
    entry=100.0,
    sl=90.0,
    tp=None,
    tp1=120.0,
    runner=130.0,
    qty=5.0,
    risk_usd=50.0,
    zone_kind="OB",
    created=SESSION_BASE,
    expires=None,
):
    return {
        "id": "ord1",
        "pair": "ETHUSD",
        "direction": direction,
        "tier": "regular",
        "zone_kind": zone_kind,
        "qty": qty,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "tp1": tp1,
        "runner_tp": runner,
        "risk_usd": risk_usd,
        "risk_dist": abs(entry - sl),
        "status": "pending",
        "outcome": None,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat() if expires else None,
        "filled_at": None,
        "tp1_at": None,
        "resolved_at": None,
        "checked_until": None,
        "fill_price": None,
        "fees_usd": 0.0,
        "pnl_usd": 0.0,
        "result_r": None,
        "equity_after": None,
    }


# Long fixtures: entry 100, SL 90, TP1 120, runner 130 (qty 5, risk $50).
FILL = candle(101, 102, 99, 101, index=1)          # trades through the entry
TOUCH = candle(101, 102, 100.0, 101, index=1)      # touches, never through
TP1_HIT = candle(101, 121, 100.5, 120.5, index=2)  # through TP1, not runner
RUNNER_HIT = candle(120, 131, 119, 130.5, index=3)
BE_HIT = candle(120, 121, 99.5, 100.5, index=3)
SL_HIT = candle(101, 102, 89, 95, index=2)


class TestEvaluateOrderLifecycle:
    def test_touch_does_not_fill_with_strict_fills(self):
        order, events = evaluate_order(_order(), [TOUCH], NOW, NOFEE)
        assert order["status"] == "pending"
        assert events == []

    def test_touch_fills_when_strict_fills_off(self):
        cfg = AutoConfig(maker_fee=0.0, taker_fee=0.0, slippage=0.0,
                         strict_fills=False)
        order, events = evaluate_order(_order(), [TOUCH], NOW, cfg)
        assert order["status"] == "open"
        assert events[0][0] == "filled"

    def test_trade_through_fills_at_the_limit_price(self):
        order, events = evaluate_order(_order(), [FILL], NOW, NOFEE)
        assert order["status"] == "open"
        assert order["fill_price"] == 100.0
        assert order["filled_at"] == FILL.timestamp.isoformat()

    def test_fill_and_stop_in_one_candle_is_a_stop(self):
        spike = candle(101, 102, 89, 95, index=1)
        order, events = evaluate_order(_order(), [spike], NOW, NOFEE)
        assert order["status"] == "closed"
        assert order["outcome"] == "sl"
        assert order["pnl_usd"] == pytest.approx(-50.0)
        assert order["result_r"] == pytest.approx(-1.0)

    def test_sl_beats_tp1_in_the_same_candle(self):
        both = candle(101, 121, 89, 95, index=2)
        order, _ = evaluate_order(_order(), [FILL, both], NOW, NOFEE)
        assert order["outcome"] == "sl"

    def test_tp1_candle_does_not_judge_be_or_runner(self):
        # The TP1 candle's low is back at the entry — judged on itself it
        # would be an instant BE. Journal semantics: the NEXT candle judges.
        tp1_and_entry = candle(101, 121, 99.9, 120.5, index=2)
        order, events = evaluate_order(
            _order(), [FILL, tp1_and_entry], NOW, NOFEE
        )
        assert order["status"] == "open_runner"
        assert [k for k, _ in events] == ["filled", "tp1"]
        # banked half at 2R: 2.5 units * $20 = $50 = +1R of the $50 risk
        assert order["pnl_usd"] == pytest.approx(50.0)

    def test_full_hybrid_run_to_the_runner(self):
        order, events = evaluate_order(
            _order(), [FILL, TP1_HIT, RUNNER_HIT], NOW, NOFEE
        )
        assert order["status"] == "closed"
        assert order["outcome"] == "tp1_runner"
        # 0.5 * 2R + 0.5 * 3R = 2.5R -> $125 on a $50 risk
        assert order["pnl_usd"] == pytest.approx(125.0)
        assert order["result_r"] == pytest.approx(2.5)

    def test_break_even_after_tp1(self):
        order, _ = evaluate_order(_order(), [FILL, TP1_HIT, BE_HIT], NOW, NOFEE)
        assert order["outcome"] == "tp1_be"
        assert order["pnl_usd"] == pytest.approx(50.0)
        assert order["result_r"] == pytest.approx(1.0)

    def test_be_beats_runner_in_the_same_candle(self):
        both = candle(120, 131, 99.5, 130, index=3)
        order, _ = evaluate_order(_order(), [FILL, TP1_HIT, both], NOW, NOFEE)
        assert order["outcome"] == "tp1_be"

    def test_non_hybrid_take_profit(self):
        order, _ = evaluate_order(
            _order(tp=140.0, tp1=None, runner=None),
            [FILL, candle(101, 141, 101, 140.5, index=2)],
            NOW,
            NOFEE,
        )
        assert order["outcome"] == "tp"
        assert order["pnl_usd"] == pytest.approx(5 * 40.0)

    def test_range_zone_kind_never_takes_the_hybrid_path(self):
        # A RANGE row with tp1 accidentally present (pre-D14 shape) must be
        # tracked on take_profit, exactly like the journal does.
        order, _ = evaluate_order(
            _order(tp=140.0, zone_kind="RANGE"),
            [FILL, candle(101, 141, 101, 140.5, index=2)],
            NOW,
            NOFEE,
        )
        assert order["outcome"] == "tp"

    def test_candle_at_expiry_cannot_fill(self):
        # GTD: the order dies AT the session end; a candle stamped at/after
        # the expiry trades through the entry and must NOT fill (stricter
        # than the journal's model, on purpose).
        expires = SESSION_BASE + timedelta(minutes=10)
        piercing = candle(101, 102, 99, 101, index=2)  # ts == expires
        order, events = evaluate_order(
            _order(expires=expires), [piercing], NOW, NOFEE
        )
        assert order["status"] == "cancelled"
        assert order["outcome"] == "expired"
        assert order["fill_price"] is None

    def test_wall_clock_expiry_without_candles(self):
        expires = SESSION_BASE + timedelta(minutes=10)
        order, events = evaluate_order(
            _order(expires=expires), [], NOW, NOFEE
        )
        assert order["status"] == "cancelled"
        assert order["outcome"] == "expired"

    def test_timeout_closes_at_market(self):
        order, _ = evaluate_order(_order(), [FILL], NOW, NOFEE)
        assert order["status"] == "open"
        late = NOW + OPEN_TIMEOUT + timedelta(hours=1)
        drift = candle(101, 102, 99, 101.5, index=2)
        order, events = evaluate_order(order, [drift], late, NOFEE)
        assert order["outcome"] == "timeout"
        # closed at the last close 101.5 -> 5 * 1.5
        assert order["pnl_usd"] == pytest.approx(7.5)


class TestEvaluateOrderCosts:
    def test_stop_loss_costs_more_than_one_r(self):
        order, _ = evaluate_order(_order(), [FILL, SL_HIT], NOW, COSTS)
        entry_fee = 5 * 100.0 * COSTS.maker_fee
        exit_price = 90.0 * (1 - COSTS.slippage)
        exit_fee = 5 * exit_price * COSTS.taker_fee
        expected = 5 * (exit_price - 100.0) - entry_fee - exit_fee
        assert order["pnl_usd"] == pytest.approx(expected)
        assert order["result_r"] == pytest.approx(expected / 50.0)
        assert order["result_r"] < -1.0  # the model's -1R is optimistic
        assert order["fees_usd"] == pytest.approx(entry_fee + exit_fee)

    def test_break_even_is_not_free(self):
        order, _ = evaluate_order(_order(), [FILL, TP1_HIT, BE_HIT], NOW, COSTS)
        entry_fee = 5 * 100.0 * COSTS.maker_fee
        tp1_fee = 2.5 * 120.0 * COSTS.maker_fee
        be_price = 100.0 * (1 - COSTS.slippage)
        be_fee = 2.5 * be_price * COSTS.taker_fee
        expected = (
            2.5 * (120.0 - 100.0)
            + 2.5 * (be_price - 100.0)
            - entry_fee
            - tp1_fee
            - be_fee
        )
        assert order["pnl_usd"] == pytest.approx(expected)
        assert order["pnl_usd"] < 50.0  # frictionless "+1R" is a ceiling

    def test_short_stop_slips_upward(self):
        short = _order(direction="short", entry=100.0, sl=110.0,
                       tp1=80.0, runner=70.0)
        fill = candle(99, 101, 98, 99, index=1)     # high > entry
        stop = candle(99, 111, 98, 105, index=2)     # high >= sl
        order, _ = evaluate_order(short, [fill, stop], NOW, COSTS)
        assert order["outcome"] == "sl"
        exit_price = 110.0 * (1 + COSTS.slippage)   # adverse = higher
        entry_fee = 5 * 100.0 * COSTS.maker_fee
        exit_fee = 5 * exit_price * COSTS.taker_fee
        expected = 5 * (100.0 - exit_price) - entry_fee - exit_fee
        assert order["pnl_usd"] == pytest.approx(expected)


# ------------------------------------------------------------------ manager


def _signal(
    *,
    id="sig1",
    direction="long",
    entry=100.0,
    sl=90.0,
    tp=None,
    tp1=120.0,
    runner=130.0,
    tier="regular",
    zone_kind="OB",
):
    return {
        "id": id,
        "pair": "ETHUSD",
        "direction": direction,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "tp1": tp1,
        "runner_tp": runner,
        "tier": tier,
        "zone_kind": zone_kind,
        "created_at": SESSION_BASE.isoformat(),
        "expires_at": (SESSION_BASE + timedelta(hours=3)).isoformat(),
    }


def _result(market=False, price=0.0):
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT, checked_at=SESSION_BASE
    )
    result.price = price
    result.setup = TradeSetup(
        direction=Direction.LONG,
        entry=100.0,
        stop_loss=90.0,
        take_profit=None,
        rr=0.0,
        fvg=FVG(0, 98.0, 100.0, True, SESSION_BASE),
        entry_is_market=market,
        tp1=120.0,
        runner_tp=130.0,
    )
    return result


def _pilot(tmp_path, cfg=None) -> Autopilot:
    db = Database(str(tmp_path / "auto.db"))
    return Autopilot(db, cfg or NOFEE)


def _resolved_row(pilot, id, outcome, result_r, when):
    row = _order()
    row.update(
        id=id,
        status="closed" if outcome not in ("expired",) else "cancelled",
        outcome=outcome,
        result_r=result_r,
        resolved_at=when.isoformat(),
    )
    pilot.orders.append(row)
    pilot._persist(row)
    return row


class TestPlacement:
    def test_sizing_from_paper_equity(self, tmp_path):
        pilot = _pilot(tmp_path)
        message = pilot.on_signal(_signal(), _result(), now=NOW)
        assert message is not None and "Paper order" in message
        order = pilot.orders[0]
        # 0.5% of $10,000 = $50 risk over a $10 stop distance -> 5 units
        assert order["risk_usd"] == pytest.approx(50.0)
        assert order["qty"] == pytest.approx(5.0)
        assert order["status"] == "pending"
        # persisted
        assert pilot.db.auto_orders_all()[0]["id"] == "sig1"

    def test_market_entry_fills_immediately_with_costs(self, tmp_path):
        pilot = _pilot(tmp_path, COSTS)
        start = pilot.equity
        message = pilot.on_signal(
            _signal(), _result(market=True, price=99.5), now=NOW
        )
        assert "market" in message
        order = pilot.orders[0]
        assert order["status"] == "open"
        assert order["fill_price"] == pytest.approx(
            99.5 * (1 + COSTS.slippage)
        )
        assert pilot.equity < start  # the taker fee left the account

    def test_pair_outside_the_roster_is_ignored(self, tmp_path):
        pilot = _pilot(tmp_path)
        signal = _signal()
        signal["pair"] = "USDJPY"
        assert pilot.on_signal(signal, _result(), now=NOW) is None
        assert pilot.orders == []

    def test_tier_filter(self, tmp_path):
        pilot = _pilot(
            tmp_path,
            AutoConfig(maker_fee=0, taker_fee=0, slippage=0, tier="star"),
        )
        assert pilot.on_signal(_signal(tier="regular"), _result(), now=NOW) is None
        assert pilot.on_signal(
            _signal(id="sig2", tier="star"), _result(), now=NOW
        ) is not None

    def test_max_open_blocks_a_second_order(self, tmp_path):
        pilot = _pilot(tmp_path)
        assert pilot.on_signal(_signal(), _result(), now=NOW) is not None
        assert pilot.on_signal(_signal(id="sig2"), _result(), now=NOW) is None

    def test_disabled_places_nothing(self, tmp_path):
        pilot = _pilot(tmp_path)
        pilot.set_enabled(False)
        assert pilot.on_signal(_signal(), _result(), now=NOW) is None
        pilot.set_enabled(True)
        assert pilot.on_signal(_signal(id="sig2"), _result(), now=NOW) is not None

    def test_no_objective_no_trade(self, tmp_path):
        pilot = _pilot(tmp_path)
        naked = _signal(tp=None, tp1=None, runner=None)
        assert pilot.on_signal(naked, _result(), now=NOW) is None


class TestRiskGates:
    def test_day_stop_blocks_and_alarms_once(self, tmp_path):
        pilot = _pilot(tmp_path)
        _resolved_row(pilot, "a", "sl", -1.0, NOW - timedelta(hours=1))
        _resolved_row(pilot, "b", "sl", -1.0, NOW - timedelta(minutes=30))
        assert pilot.on_signal(_signal(id="c"), _result(), now=NOW) is None
        first = pilot.advance("ETHUSD", [], now=NOW)
        assert any("day-stop" in m for m in first)
        again = pilot.advance("ETHUSD", [], now=NOW)
        assert not any("day-stop" in m for m in again)

    def test_day_stop_cancels_pending_orders(self, tmp_path):
        pilot = _pilot(tmp_path)
        pilot.on_signal(_signal(id="live"), _result(), now=NOW)
        _resolved_row(pilot, "a", "sl", -1.0, NOW - timedelta(hours=1))
        _resolved_row(pilot, "b", "sl", -1.0, NOW - timedelta(minutes=30))
        pilot.advance("ETHUSD", [], now=NOW)
        live = next(o for o in pilot.orders if o["id"] == "live")
        assert live["status"] == "cancelled"
        assert live["outcome"] == "day_stop"

    def test_yesterday_stops_do_not_close_today(self, tmp_path):
        pilot = _pilot(tmp_path)
        _resolved_row(pilot, "a", "sl", -1.0, NOW - timedelta(days=2))
        _resolved_row(pilot, "b", "sl", -1.0, NOW - timedelta(days=2))
        assert pilot.on_signal(_signal(id="c"), _result(), now=NOW) is not None

    def test_weekly_kill_blocks_until_overridden(self, tmp_path):
        pilot = _pilot(tmp_path)
        _resolved_row(pilot, "a", "timeout", -3.5, NOW - timedelta(hours=2))
        _resolved_row(pilot, "b", "timeout", -3.2, NOW - timedelta(hours=1))
        assert pilot.week_r(NOW) == pytest.approx(-6.7)
        assert pilot.on_signal(_signal(id="c"), _result(), now=NOW) is None
        alarms = pilot.advance("ETHUSD", [], now=NOW)
        assert any("weekly kill" in m for m in alarms)
        # /auto on is the explicit D19 override for the current week
        note = pilot.set_enabled(True, now=NOW)
        assert "OVERRIDDEN" in note
        assert pilot.on_signal(_signal(id="c"), _result(), now=NOW) is not None


class TestAdvanceEquity:
    def test_full_lifecycle_moves_the_paper_equity(self, tmp_path):
        pilot = _pilot(tmp_path)
        pilot.on_signal(_signal(), _result(), now=NOW)
        messages = pilot.advance(
            "ETHUSD", [FILL, TP1_HIT, RUNNER_HIT], now=NOW
        )
        kinds = "\n".join(messages)
        assert "filled" in kinds and "TP1" in kinds
        order = pilot.orders[0]
        assert order["outcome"] == "tp1_runner"
        assert pilot.equity == pytest.approx(10125.0)  # +2.5R of $50
        assert order["equity_after"] == pytest.approx(10125.0)

    def test_config_from_settings_converts_percent_to_fractions(self):
        from app.core.config import SMCSettings

        cfg = AutoConfig.from_settings(SMCSettings())
        assert cfg.maker_fee == pytest.approx(0.0002)
        assert cfg.taker_fee == pytest.approx(0.0005)
        assert cfg.slippage == pytest.approx(0.0002)
        assert cfg.pairs == ["ETHUSD"]
        assert cfg.risk_pct == pytest.approx(0.5)


# ------------------------------------------------------------------- wiring


class _FakeNotifier:
    def __init__(self, fail_sends=False):
        self.sent = []
        self.fail_sends = fail_sends

    async def send(self, text, **kwargs):
        if self.fail_sends:
            return None
        self.sent.append(text)
        return len(self.sent)

    async def pin(self, message_id):
        pass


def _engine_result():
    from app.services.smc.engine import TripleSyncEngine
    from tests.test_smc.helpers import (
        H1_PULLBACK_CLOSES,
        H4_UPTREND_CLOSES,
        m5_long_trigger_deep_sweep,
        make_candles,
    )

    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )
    result.session_name = "New York"
    engine = TripleSyncEngine(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0
    )
    return engine.evaluate(
        h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5=m5_long_trigger_deep_sweep(), result=result,
    )


def _watcher(tmp_path, *, fail_sends=False, with_autopilot=True):
    from smc_watcher import Watcher

    async def _no_chart(*args, **kwargs):
        return None

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    watcher.journal = SignalJournal(db)
    watcher.notifier = _FakeNotifier(fail_sends=fail_sends)
    watcher._send_chart = _no_chart
    if with_autopilot:
        watcher.autopilot = Autopilot(db, NOFEE)
    return watcher


class TestWatcherWiring:
    @pytest.mark.asyncio
    async def test_recorded_signal_places_a_paper_order(self, tmp_path):
        watcher = _watcher(tmp_path)
        result = _engine_result()
        assert result.setup is not None
        sent = await watcher._send_alert("ETHUSD", result, "fp")
        assert sent is True
        assert len(watcher.autopilot.orders) == 1
        assert watcher.autopilot.orders[0]["id"] == watcher.journal.signals[0]["id"]
        assert any("Paper order" in m for m in watcher.notifier.sent)

    @pytest.mark.asyncio
    async def test_failed_alert_send_keeps_the_signal_row(self, tmp_path):
        # The paper order references the signal row; a Telegram outage must
        # not discard it, and the dedup fingerprint must advance so the next
        # cycle does not double-record the setup.
        watcher = _watcher(tmp_path, fail_sends=True)
        result = _engine_result()
        sent = await watcher._send_alert("ETHUSD", result, "fp")
        assert sent is False
        assert len(watcher.autopilot.orders) == 1
        assert len(watcher.journal.signals) == 1
        assert watcher.state.last_setup["ETHUSD"] == "fp"

    @pytest.mark.asyncio
    async def test_failed_send_without_autopilot_still_discards(self, tmp_path):
        watcher = _watcher(tmp_path, fail_sends=True, with_autopilot=False)
        result = _engine_result()
        sent = await watcher._send_alert("ETHUSD", result, "fp")
        assert sent is False
        assert watcher.journal.signals == []
        assert "ETHUSD" not in watcher.state.last_setup

    @pytest.mark.asyncio
    async def test_muted_notify_level_still_trades_and_reports(self, tmp_path):
        watcher = _watcher(tmp_path)
        watcher.state.set_notify_level("mute")
        result = _engine_result()
        sent = await watcher._send_alert("ETHUSD", result, "fp")
        assert sent is False  # the setup alert really was muted
        assert len(watcher.autopilot.orders) == 1
        assert any("Paper order" in m for m in watcher.notifier.sent)

    @pytest.mark.asyncio
    async def test_track_journal_advances_paper_orders(
        self, tmp_path, monkeypatch
    ):
        import smc_watcher as watcher_module

        watcher = _watcher(tmp_path)
        pilot = watcher.autopilot
        pilot.on_signal(_signal(), _result(), now=NOW)

        class _StubFetcher:
            async def fetch_candles(self, interval, limit=400):
                return [FILL, TP1_HIT, RUNNER_HIT]

        monkeypatch.setattr(
            watcher_module, "_build_fetcher", lambda instrument: _StubFetcher()
        )
        await watcher._track_journal()
        assert pilot.orders[0]["outcome"] == "tp1_runner"
        assert any("runner target hit" in m for m in watcher.notifier.sent)

    def test_auto_command_paths(self, tmp_path):
        watcher = _watcher(tmp_path)
        assert "Autopilot" in watcher.auto_command("")
        assert "OFF" in watcher.auto_command("off")
        assert "ON" in watcher.auto_command("on")
        assert "no orders yet" in watcher.auto_command("report")
        watcher.autopilot.on_signal(_signal(), _result(), now=NOW)
        assert "paper track" in watcher.auto_command("report")

    def test_auto_command_without_module(self, tmp_path):
        watcher = _watcher(tmp_path, with_autopilot=False)
        assert "SMC_AUTO_TRADE" in watcher.auto_command("")

    def test_status_lines_render(self, tmp_path):
        pilot = _pilot(tmp_path)
        lines = pilot.status_lines(NOW)
        assert any("Autopilot (paper): ON" in line for line in lines)
