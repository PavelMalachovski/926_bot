"""Tests for the auto-plan feature: settings, state plumbing, summary
formatting, snapshot gate, per-cycle recompute/edit and the plan-zone alert."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings, SMCSettings
from app.services.smc.db import Database
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.notifier import (
    format_plan_summary,
    format_zone_alert,
    plan_summary_keyboard,
)
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.planbook import PlanBook, PlanEntry, plan_fingerprint
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    make_candles,
)


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
        for var in (
            "SMC_AUTO_PLAN", "SMC_AUTO_PLAN_TIMES", "SMC_ZONE_PING",
            "SMC_PD_ALERT", "SMC_PLAN_CHANGE_ALERT",
        ):
            monkeypatch.delenv(var, raising=False)
        s = SMCSettings()
        assert s.auto_plan is True
        assert s.auto_plan_times == "08:05,14:05"
        # D25 (owner decision 2026-09-05): no get-ready messages at all —
        # the plan-zone alert, the PD radar and the 🔁 plan-updated message
        # are legacy, off unless re-enabled.
        assert s.zone_ping is False
        assert s.pd_alert is False
        assert s.plan_change_alert is False


class TestAutoPlanSlotsParsing:
    """_autoplan_slots: validated 'HH:MM' Prague slots from the raw
    SMC_AUTO_PLAN_TIMES setting — invalid/out-of-range entries are
    dropped, duplicates collapse, and the result is sorted."""

    def _watcher(self):
        return _stub_watcher()

    def test_invalid_entries_dropped_valid_kept(self, monkeypatch):
        w = self._watcher()
        monkeypatch.setattr(
            settings.smc, "auto_plan_times", "07:55, 25:99, garbage, 13:55"
        )
        assert w._autoplan_slots() == ["07:55", "13:55"]

    def test_empty_string_yields_no_slots(self, monkeypatch):
        w = self._watcher()
        monkeypatch.setattr(settings.smc, "auto_plan_times", "")
        assert w._autoplan_slots() == []

    def test_duplicates_collapse_and_sort(self, monkeypatch):
        w = self._watcher()
        monkeypatch.setattr(
            settings.smc, "auto_plan_times", "13:55,07:55,13:55,07:55"
        )
        assert w._autoplan_slots() == ["07:55", "13:55"]


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
        db.kv_set("zone_pinged", {
            "ETHUSD": True,
            "USDJPY": [1.0, 2.0],  # wrong-length list: also dropped
            "EURUSD": [1.0, 2.0, "long", "2026-08-11"],  # flat 4-list: legacy, dropped
        })
        state = WatcherState(db)
        assert "ETHUSD" not in state.zone_pinged
        assert "USDJPY" not in state.zone_pinged
        assert "EURUSD" not in state.zone_pinged


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


class _StubState:
    # The zone dedup/mute methods are borrowed from the real WatcherState:
    # they touch nothing but zone_pinged/zone_muted and save(), and a
    # hand-written copy here would silently drift from production.
    from app.services.smc.state import WatcherState as _Real

    zone_already_pinged = _Real.zone_already_pinged
    remember_zone_ping = _Real.remember_zone_ping
    zone_muted_until = _Real.zone_muted_until
    mute_zone_alerts = _Real.mute_zone_alerts
    del _Real

    def __init__(self):
        self.pairs = ["ETHUSD"]
        self.pair_profile = {}
        self.pair_cooldown = {}
        self.zone_pinged = {}
        self.zone_muted = {}
        self.auto_plan_sent = {}
        self.plan_summary = {}
        self.plan_zones = {}
        self.plan_zones_date = ""
        self.paused = False

    def save(self):
        pass

    def remember_plan_zones(self, key, zones, now=None):
        from app.services.smc.state import WatcherState
        self.plan_zones[key.upper()] = [
            WatcherState._normalise_zone(z) for z in zones
        ]
        self.plan_zones_date = WatcherState._prague_day(now)


class _StubNotifier:
    def __init__(self):
        self.sent = []          # (text, reply_markup, disable_notification)
        self.edited = []        # (message_id, text)
        self.photos = []
        self.fail_sends = False

    async def send(self, text, reply_markup=None, disable_notification=False):
        if self.fail_sends:
            return None
        self.sent.append((text, reply_markup, disable_notification))
        return len(self.sent)

    async def edit_message(self, message_id, text, reply_markup=None):
        self.edited.append((message_id, text))
        return True

    async def send_photo(self, photo, caption=None, reply_to=None):
        self.photos.append(photo)
        return 99


def _stub_watcher():
    from smc_watcher import Watcher

    w = Watcher.__new__(Watcher)
    w.state = _StubState()
    w.notifier = _StubNotifier()
    w.planbook = PlanBook()
    return w


def _fetched_entry(pair="ETHUSD", scenarios=None):
    return PlanEntry(
        plan=_pair_plan(pair, scenarios or [_scenario()]),
        data={"h4": [], "h1": [], "m5": []},
        as_of="07:54",
    )


class TestAutoPlanGate:
    """_maybe_auto_plan: once per slot per Prague day, most recent slot only."""

    def _watcher_with_fake_snapshot(self, monkeypatch):
        w = _stub_watcher()
        w.snapshots = []

        async def fake_snapshot(slot):
            w.snapshots.append(slot)
            return True

        w._auto_plan_snapshot = fake_snapshot
        monkeypatch.setattr(settings.smc, "auto_plan", True)
        monkeypatch.setattr(settings.smc, "auto_plan_times", "07:55,13:55")
        return w

    def test_fires_most_recent_due_slot_once(self, monkeypatch):
        import smc_watcher as mod
        w = self._watcher_with_fake_snapshot(monkeypatch)
        # 15:00 Prague = 13:00 UTC in July (CEST): both slots due
        fake_now = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        monkeypatch.setattr(mod, "datetime", _DT)
        asyncio.run(w._maybe_auto_plan())
        assert w.snapshots == ["13:55"]          # morning slot skipped as stale
        assert w.state.auto_plan_sent["13:55"]   # marked
        assert w.state.auto_plan_sent["07:55"]   # stale slot consumed too
        asyncio.run(w._maybe_auto_plan())
        assert w.snapshots == ["13:55"]          # no re-fire same day

    def test_nothing_before_first_slot(self, monkeypatch):
        import smc_watcher as mod
        w = self._watcher_with_fake_snapshot(monkeypatch)
        fake_now = datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)  # 06:00 Prague

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        monkeypatch.setattr(mod, "datetime", _DT)
        asyncio.run(w._maybe_auto_plan())
        assert w.snapshots == []

    def test_disabled_by_flag(self, monkeypatch):
        w = self._watcher_with_fake_snapshot(monkeypatch)
        monkeypatch.setattr(settings.smc, "auto_plan", False)
        asyncio.run(w._maybe_auto_plan())
        assert w.snapshots == []

    def test_failed_send_retries_next_cycle(self, monkeypatch):
        import smc_watcher as mod
        w = _stub_watcher()
        calls = []

        async def failing_snapshot(slot):
            calls.append(slot)
            return False

        w._auto_plan_snapshot = failing_snapshot
        monkeypatch.setattr(settings.smc, "auto_plan", True)
        monkeypatch.setattr(settings.smc, "auto_plan_times", "07:55")
        fake_now = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)  # 08:00 Prague

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        monkeypatch.setattr(mod, "datetime", _DT)
        asyncio.run(w._maybe_auto_plan())
        asyncio.run(w._maybe_auto_plan())
        assert calls == ["07:55", "07:55"]  # unmarked -> retried


class TestAutoPlanSnapshot:
    def _watcher(self, monkeypatch, pairs=("ETHUSD",)):
        w = _stub_watcher()
        w.state.pairs = list(pairs)

        async def fake_fetch(key, force_fresh=True):
            entry = _fetched_entry(key)
            w.planbook.update(key, entry)
            return entry

        w._fetch_pair_plan = fake_fetch
        return w

    def test_snapshot_sends_silent_summary_and_remembers_zones(self, monkeypatch):
        w = self._watcher(monkeypatch)
        ok = asyncio.run(w._auto_plan_snapshot("07:55"))
        assert ok is True
        text, markup, silent = w.notifier.sent[0]
        assert silent is True
        assert "Pre-Market Plan 07:55" in text
        assert any(
            "aplan_ETHUSD" in str(row) for row in markup["inline_keyboard"]
        )
        # snapshot writes provenance (spec §4)
        assert "ETHUSD" in w.state.plan_zones
        # summary state stored for later edits
        assert w.state.plan_summary["slot"] == "07:55"
        assert "ETHUSD" in w.state.plan_summary["fingerprints"]

    def test_weekend_skips_forex_keeps_crypto(self, monkeypatch):
        import smc_watcher as mod
        w = self._watcher(monkeypatch, pairs=("ETHUSD", "EURUSD"))
        # Saturday 2026-07-04, 07:55 Prague
        fake_now = datetime(2026, 7, 4, 5, 55, tzinfo=timezone.utc)

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now

        monkeypatch.setattr(mod, "datetime", _DT)
        asyncio.run(w._auto_plan_snapshot("07:55"))
        text = w.notifier.sent[0][0]
        assert "ETHUSD" in text and "EURUSD" not in text

    def test_failed_summary_send_returns_false(self, monkeypatch):
        w = self._watcher(monkeypatch)
        w.notifier.fail_sends = True
        assert asyncio.run(w._auto_plan_snapshot("07:55")) is False
        assert w.state.plan_summary == {}


def _result_with_candles(price=3160.0, verdict=Verdict.WATCH):
    r = AnalysisResult(
        symbol="ETHUSD", verdict=verdict,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
        price_decimals=2,
    )
    r.session_name = "New York"
    r.h4_candles = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    r.h1_candles = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    r.m5_candles = make_candles([price], step_minutes=5)
    return r


class TestRecompute:
    def test_recompute_fills_planbook_without_provenance(self):
        w = _stub_watcher()
        w._recompute_plan("ETHUSD", _result_with_candles())
        entry = w.planbook.get("ETHUSD")
        assert entry is not None and entry.plan.pair == "ETHUSD"
        assert w.state.plan_zones == {}  # spec §4: recompute never writes it

    def test_no_candles_no_recompute(self):
        w = _stub_watcher()
        r = _result_with_candles()
        r.h4_candles = None
        w._recompute_plan("ETHUSD", r)
        assert w.planbook.get("ETHUSD") is None

    def test_off_session_result_skipped(self):
        w = _stub_watcher()
        w._recompute_plan("ETHUSD", _result_with_candles(verdict=Verdict.OFF_SESSION))
        assert w.planbook.get("ETHUSD") is None

    def test_engine_analyze_keeps_h4_h1(self):
        # the recompute's data source: analyze() must expose what it fetched
        import asyncio as aio
        from app.services.smc.engine import TripleSyncEngine

        class _Fetcher:
            async def fetch_all_timeframes(self, force_fresh=False):
                return {
                    "h4": make_candles(H4_UPTREND_CLOSES, step_minutes=240),
                    "h1": make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
                    "m5": make_candles(
                        [3160.0],
                        start=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
                    ),
                }

            async def fetch_funding_rate(self):
                # default instrument is ETHUSD (check_funding=True) —
                # analyze() calls this after stashing h4/h1/m5 candles.
                return None

        engine = TripleSyncEngine(fetcher=_Fetcher(), enforce_sessions=False)
        result = aio.run(engine.analyze())
        assert result.h4_candles and result.h1_candles and result.m5_candles


class TestSummaryEdit:
    def _watcher_with_summary(self):
        w = _stub_watcher()
        entry = _fetched_entry("ETHUSD")
        w.planbook.update("ETHUSD", entry)
        w.state.plan_summary = {
            "message_id": 42, "slot": "07:55",
            # date must be the *Prague* day — reuse the state helper
            "date": WatcherState._prague_day(),
            "fingerprints": {"ETHUSD": plan_fingerprint(entry.plan)},
        }
        return w

    def test_no_edit_when_plan_unchanged(self):
        w = self._watcher_with_summary()
        asyncio.run(w._maybe_edit_plan_summary())
        assert w.notifier.edited == []

    def test_material_change_edits_with_upd(self):
        w = self._watcher_with_summary()
        moved = _fetched_entry("ETHUSD", [_scenario(bottom=3140.0, top=3145.0)])
        w.planbook.update("ETHUSD", moved)
        asyncio.run(w._maybe_edit_plan_summary())
        assert len(w.notifier.edited) == 1
        message_id, text = w.notifier.edited[0]
        assert message_id == 42 and "upd " in text and "3140.00–3145.00" in text
        # stored fingerprints refreshed -> second pass is a no-op
        asyncio.run(w._maybe_edit_plan_summary())
        assert len(w.notifier.edited) == 1

    def test_stale_summary_from_yesterday_ignored(self):
        w = self._watcher_with_summary()
        w.state.plan_summary["date"] = "2020-01-01"
        w.planbook.update("ETHUSD", _fetched_entry("ETHUSD", [_scenario(bottom=1.0, top=2.0)]))
        asyncio.run(w._maybe_edit_plan_summary())
        assert w.notifier.edited == []


class TestSummaryEditMissingPair:
    """A pair missing from the planbook (mid-day restart racing a failing
    feed, or the pair got disabled) must degrade alone — it should never
    freeze summary edits for the pairs that ARE fresh."""

    def _watcher_with_one_missing(self):
        w = _stub_watcher()
        eth_entry = _fetched_entry("ETHUSD")
        w.planbook.update("ETHUSD", eth_entry)
        # EURUSD is intentionally absent from the book: the missing-pair case
        w.state.plan_summary = {
            "message_id": 42, "slot": "07:55",
            "date": WatcherState._prague_day(),
            "fingerprints": {
                "ETHUSD": plan_fingerprint(eth_entry.plan),
                "EURUSD": "stale-fingerprint",
            },
        }
        return w

    def test_missing_pair_alone_does_not_trigger_edit(self):
        w = self._watcher_with_one_missing()
        asyncio.run(w._maybe_edit_plan_summary())
        assert w.notifier.edited == []

    def test_other_pair_change_edits_with_placeholder_for_missing(self):
        w = self._watcher_with_one_missing()
        moved = _fetched_entry("ETHUSD", [_scenario(bottom=3140.0, top=3145.0)])
        w.planbook.update("ETHUSD", moved)
        asyncio.run(w._maybe_edit_plan_summary())
        assert len(w.notifier.edited) == 1
        _, text = w.notifier.edited[0]
        assert "3140.00–3145.00" in text
        assert "EURUSD" in text and "no fresh plan" in text
        # the missing pair's stored fingerprint carries forward unchanged
        assert w.state.plan_summary["fingerprints"]["EURUSD"] == "stale-fingerprint"


class TestPlanZoneAlert:
    """The plan-centric zone alert (spec §5, dedup rule per owner decision
    2026-08-16 §1.3): fires once per zone per session block when the last
    closed M5 candle overlaps a scenario zone of the CURRENT plan, quoting
    the plan's numbers. The old engine-zone ping is gone."""

    def _armed_watcher(self, monkeypatch, price=3137.0):
        monkeypatch.setattr(settings.smc, "zone_ping", True)
        w = _stub_watcher()
        w.planbook.update("ETHUSD", _fetched_entry("ETHUSD"))  # zone 3131-3138
        return w, _result_with_candles(price=price)

    def test_touch_alerts_with_plan_numbers_once(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))  # same block, no re-arm
        assert len(w.notifier.sent) == 1
        text = w.notifier.sent[0][0]
        assert "🔔" in text and "Buy Limit 3138.00" in text

    def test_no_alert_outside_zone(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch, price=3160.0)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert w.notifier.sent == []

    def test_exit_and_reentry_stays_silent(self, monkeypatch):
        """Owner decision 2026-08-16 (reverses spec 2026-08-11 §5): price
        leaving the zone no longer re-arms the alert. This is the exact
        USDCAD 2026-08-13 failure — four identical messages while price
        oscillated on the zone edge."""
        w, r = self._armed_watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        away = _result_with_candles(price=3160.0)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", away))  # left the zone
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))     # re-entry
        assert len(w.notifier.sent) == 1

    def test_overlapping_replacement_zone_stays_silent(self, monkeypatch):
        """A zone whose bounds drifted between plan recomputes is the same
        trading idea (D2), so it does not earn a second alert."""
        w, r = self._armed_watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        w.planbook.update(
            "ETHUSD", _fetched_entry("ETHUSD", [_scenario(bottom=3135.0, top=3141.0)])
        )
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert len(w.notifier.sent) == 1

    def test_off_session_is_silent(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch)
        r.session_name = None
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert w.notifier.sent == []

    def test_approved_verdict_is_covered_by_full_alert(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch)
        r.verdict = Verdict.APPROVED_LIMIT
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert w.notifier.sent == []

    def test_cooldown_suppresses(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch)
        w.state.pair_cooldown["ETHUSD"] = (
            datetime.now(tz=timezone.utc) + timedelta(hours=2)
        ).isoformat()
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert w.notifier.sent == []

    def test_send_failure_keeps_episode_unmarked(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch)
        w.notifier.fail_sends = True
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        w.notifier.fail_sends = False
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert len(w.notifier.sent) == 1  # retried and delivered

    def test_disabled_by_flag(self, monkeypatch):
        w, r = self._armed_watcher(monkeypatch)
        monkeypatch.setattr(settings.smc, "zone_ping", False)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert w.notifier.sent == []


def _live_entry(pair="ETHUSD", price=3160.0):
    """A PlanEntry with REAL synthetic candles, so the pure checklist can run
    on it the way the Setup-analysis button does (H4 uptrend, H1 demand zone
    3131-3138, price above the zone -> WATCH, pullback phase)."""
    data = {
        "h4": make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        "h1": make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        "m5": make_candles([price], step_minutes=5),
    }
    return PlanEntry(plan=_pair_plan(pair, [_scenario()]), data=data, as_of="07:54")


class TestStrategyAuditButton:
    """D25 (owner decision 2026-09-05): the audit is COMPUTED on schedule —
    with the 08:05/14:05 snapshot and on every cycle's recompute — and the
    aplan_* buttons only DELIVER it, so a press costs zero API calls. Only an
    empty book (a restart before any cycle) fetches."""

    def _watcher(self, monkeypatch, fresh_entry=None):
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda *a, **k: None)
        w = _stub_watcher()
        w.fetches = []

        async def fake_fetch(key, force_fresh=True):
            w.fetches.append((key, force_fresh))
            return fresh_entry

        w._fetch_pair_plan = fake_fetch
        return w

    @staticmethod
    def _engine_result(price=3160.0):
        """What `_recompute_plan` really receives: the engine's own evaluated
        result (Rule 1/2 done, `h1_zone` set), not a bare WATCH shell."""
        from app.services.smc.engine import TripleSyncEngine

        r = _result_with_candles(price=price)
        r.price = price
        return TripleSyncEngine(max_entry_gap_r=99.0).evaluate(
            h4=r.h4_candles, h1=r.h1_candles, m5=r.m5_candles, result=r
        )

    def test_recompute_stores_the_audit_with_the_plan(self):
        w = _stub_watcher()
        w._recompute_plan("ETHUSD", self._engine_result())
        entry = w.planbook.get("ETHUSD")
        assert entry.result is not None and entry.audit is not None
        assert entry.audit.main is not None
        assert entry.audit.main.entry == 3138.0  # the H1 demand zone edge

    def test_press_delivers_the_stored_audit_without_fetching(self, monkeypatch):
        w = self._watcher(monkeypatch)
        w._recompute_plan("ETHUSD", self._engine_result())
        asyncio.run(w.on_setup_analysis("ETHUSD"))
        assert w.fetches == []  # zero API calls
        text = w.notifier.sent[0][0]
        assert "Strategy audit — ETHUSD" in text
        assert "Pending (limit) entries" in text
        assert "MAIN" in text and "3138.00" in text
        assert "<pre>" in text and "</pre>" in text

    def test_empty_book_fetches_once(self, monkeypatch):
        entry = _live_entry()
        entry.result = self._engine_result()
        from app.services.smc.pending import build_pending
        from app.services.smc.instruments import get_instrument

        entry.audit = build_pending(
            entry.result, get_instrument("ETHUSD"),
            entry.data["h4"], entry.data["h1"], entry.data["m5"],
        )
        w = self._watcher(monkeypatch, entry)
        asyncio.run(w.on_setup_analysis("ETHUSD"))
        assert w.fetches == [("ETHUSD", True)]
        assert "Strategy audit — ETHUSD" in w.notifier.sent[0][0]

    def test_entry_without_an_audit_falls_back_to_the_plan_text(self, monkeypatch):
        w = self._watcher(monkeypatch)
        w.planbook.update("ETHUSD", _fetched_entry("ETHUSD"))  # no audit
        delivered = []

        async def fake_deliver(key, entry):
            delivered.append(key)

        w._deliver_plan = fake_deliver
        asyncio.run(w.on_setup_analysis("ETHUSD"))
        assert delivered == ["ETHUSD"] and w.notifier.sent == []

    def test_failed_fetch_on_an_empty_book_sends_nothing(self, monkeypatch):
        w = self._watcher(monkeypatch, None)
        asyncio.run(w.on_setup_analysis("ETHUSD"))
        assert w.notifier.sent == []

    def test_all_serves_every_enabled_pair(self, monkeypatch):
        w = self._watcher(monkeypatch, _live_entry())
        w.state.pairs = ["ETHUSD", "USDJPY"]
        served = []

        async def fake_analysis(key):
            served.append(key)

        w._send_setup_analysis = fake_analysis
        asyncio.run(w.on_setup_analysis("ALL"))
        assert served == ["ETHUSD", "USDJPY"]


class TestAplanCallback:
    """Task 4 (review-hardening): the aplan_/plan_ callback handlers spawn
    on_stored_plan/on_plan as a background task (asyncio.create_task)
    instead of awaiting them inline — a slow plan build (12 pairs x
    force-fresh fetch + chart render) must not block getUpdates for the
    ~90s it used to. The Watcher's own `_get_cycle_lock()` still serializes
    the actual work against a running cycle or another plan build."""

    @pytest.mark.asyncio
    async def test_aplan_callback_spawns_the_handler_in_the_background(self):
        from app.services.smc.telegram_bot import TelegramCommandBot

        calls = []

        async def on_setup_analysis(key):
            calls.append(key)

        bot = TelegramCommandBot.__new__(TelegramCommandBot)
        bot.owner_chat_id = "1"
        bot.on_setup_analysis = on_setup_analysis
        api_calls = []

        async def fake_api(method, **payload):
            api_calls.append(method)
            return {}

        bot._api = fake_api
        callback = {
            "id": "cb1",
            "from": {"id": 1},
            "message": {"chat": {"id": 1}, "message_id": 5},
            "data": "aplan_ETHUSD",
        }
        await bot._handle_callback(callback)
        # handler returned before the fire-and-forget task ever ran
        assert calls == []
        assert "answerCallbackQuery" in api_calls
        # ... but a task really was spawned, and it does complete
        assert bot._background_tasks
        await asyncio.gather(*bot._background_tasks)
        assert calls == ["ETHUSD"]

    @pytest.mark.asyncio
    async def test_plan_callback_returns_before_a_slow_plan_build_finishes(
        self,
    ):
        """Regression test (c): the polling handler must not await the full
        plan build — proven with a slow fake on_plan that records when it
        starts and ends."""
        from app.services.smc.telegram_bot import TelegramCommandBot

        finished = asyncio.Event()
        order = []

        async def slow_on_plan(key):
            order.append("start")
            await asyncio.sleep(0.05)
            order.append("end")
            finished.set()

        bot = TelegramCommandBot.__new__(TelegramCommandBot)
        bot.owner_chat_id = "1"
        bot.on_plan = slow_on_plan

        async def fake_api(method, **payload):
            return {}

        bot._api = fake_api
        callback = {
            "id": "cb1",
            "from": {"id": 1},
            "message": {"chat": {"id": 1}, "message_id": 5},
            "data": "plan_ETHUSD",
        }
        await bot._handle_callback(callback)
        # the slow build has not even started, let alone finished, by the
        # time the handler gives control back to the polling loop
        assert order == []
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert order == ["start", "end"]
