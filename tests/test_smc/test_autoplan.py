"""Tests for the auto-plan feature: settings, state plumbing, summary
formatting, snapshot gate, per-cycle recompute/edit and the plan-zone alert."""

from datetime import datetime, timezone

from app.core.config import SMCSettings
from app.services.smc.db import Database
from app.services.smc.models import Direction, Trend
from app.services.smc.notifier import (
    format_plan_summary,
    format_zone_alert,
    plan_summary_keyboard,
)
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.state import WatcherState


def _scenario(direction=Direction.LONG, bottom=3131.0, top=3138.0,
              rr=2.1, speculative=False):
    entry = top if direction == Direction.LONG else bottom
    return PlanScenario(
        direction=direction, entry=entry, stop_loss=bottom - 2.0,
        take_profit=top + 20.0, rr=rr, zone_bottom=bottom, zone_top=top,
        speculative=speculative,
    )


def _pair_plan(pair="ETHUSD", scenarios=None, blocker=None, closed=False):
    plan = PairPlan(pair=pair, price=3160.0, price_decimals=2,
                    h4_trend=Trend.UP, scenarios=scenarios or [],
                    market_closed=closed)
    plan.blocker = blocker
    return plan


class TestAutoPlanSettings:
    def test_defaults(self, monkeypatch):
        for var in ("SMC_AUTO_PLAN", "SMC_AUTO_PLAN_TIMES", "SMC_ZONE_PING"):
            monkeypatch.delenv(var, raising=False)
        s = SMCSettings()
        assert s.auto_plan is True
        assert s.auto_plan_times == "07:55,13:55"
        # the plan-zone alert is the feature's main notification now
        assert s.zone_ping is True


class TestStatePlumbing:
    def _state(self, tmp_path):
        return WatcherState(Database(str(tmp_path / "t.db")))

    def test_auto_plan_state_round_trips(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        state = WatcherState(db)
        state.auto_plan_sent["07:55"] = "2026-08-11"
        state.plan_summary = {
            "message_id": 5, "slot": "07:55", "date": "2026-08-11",
            "fingerprints": {"ETHUSD": "fp"},
        }
        state.save()
        reloaded = WatcherState(Database(str(tmp_path / "t.db")))
        assert reloaded.auto_plan_sent == {"07:55": "2026-08-11"}
        assert reloaded.plan_summary["message_id"] == 5

    def test_legacy_bool_zone_pinged_is_dropped(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.kv_set("zone_pinged", {"ETHUSD": True, "EURUSD": [1.0, 2.0, "long", "2026-08-11"]})
        state = WatcherState(db)
        assert "ETHUSD" not in state.zone_pinged
        assert state.zone_pinged["EURUSD"] == [1.0, 2.0, "long", "2026-08-11"]


class TestSilentSend:
    def test_send_passes_disable_notification(self, monkeypatch):
        import asyncio
        from app.services.smc.notifier import TelegramNotifier

        captured = {}

        async def fake_api(self, method, **payload):
            captured.update(payload)
            return {"message_id": 7}

        monkeypatch.setattr(TelegramNotifier, "_api", fake_api)
        n = TelegramNotifier(bot_token="123:x", chat_id="1")
        mid = asyncio.run(n.send("hi", disable_notification=True))
        assert mid == 7 and captured.get("disable_notification") is True

    def test_send_default_is_audible(self, monkeypatch):
        import asyncio
        from app.services.smc.notifier import TelegramNotifier

        captured = {}

        async def fake_api(self, method, **payload):
            captured.update(payload)
            return {"message_id": 7}

        monkeypatch.setattr(TelegramNotifier, "_api", fake_api)
        n = TelegramNotifier(bot_token="123:x", chat_id="1")
        asyncio.run(n.send("hi"))
        assert "disable_notification" not in captured


class TestPlanSummaryFormat:
    def test_scenario_line(self):
        text = format_plan_summary("07:55", [_pair_plan(scenarios=[_scenario()])])
        assert "Pre-Market Plan 07:55" in text
        assert "ETHUSD" in text and "LONG" in text
        assert "3131.00–3138.00" in text and "1:2.1" in text
        assert "speculative" not in text

    def test_speculative_marker_and_blocker_line(self):
        plans = [
            _pair_plan("ETHUSD", [_scenario(speculative=True)]),
            _pair_plan("EURUSD", blocker="fill < 50% of the zone"),
        ]
        text = format_plan_summary("13:55", plans)
        assert "(speculative)" in text
        # blocker is dynamic text -> must be escaped
        assert "fill &lt; 50%" in text and "fill < 50%" not in text

    def test_updated_suffix(self):
        text = format_plan_summary("07:55", [_pair_plan()], updated_hhmm="10:15")
        assert "upd 10:15" in text

    def test_market_closed_row(self):
        text = format_plan_summary("07:55", [_pair_plan(closed=True)])
        assert "market closed" in text


class TestPlanSummaryKeyboard:
    def test_buttons_per_pair_plus_all(self):
        kb = plan_summary_keyboard(["ETHUSD", "EURUSD", "USDJPY"])
        flat = [b for row in kb["inline_keyboard"] for b in row]
        datas = [b["callback_data"] for b in flat]
        assert "aplan_ETHUSD" in datas and "aplan_USDJPY" in datas
        assert datas[-1] == "aplan_ALL"


class TestZoneAlertFormat:
    def test_long_alert_carries_plan_numbers(self):
        text = format_zone_alert("ETHUSD", _scenario(), 2)
        assert text.startswith("🔔")
        assert "Demand zone 3131.00–3138.00" in text
        assert "Buy Limit 3138.00" in text
        assert "SL 3129.00" in text and "TP 3158.00" in text
        assert "1:2.1" in text
        assert "bullish CHoCH + FVG" in text

    def test_short_speculative_alert(self):
        s = _scenario(direction=Direction.SHORT, speculative=True)
        text = format_zone_alert("EURUSD", s, 2)
        assert "Supply zone" in text and "Sell Limit 3131.00" in text
        assert "(speculative)" in text and "bearish" in text
