"""Tests for the auto-plan feature: settings, state plumbing, summary
formatting, snapshot gate, per-cycle recompute/edit and the plan-zone alert."""

from datetime import datetime, timezone

from app.core.config import SMCSettings
from app.services.smc.db import Database
from app.services.smc.state import WatcherState


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
