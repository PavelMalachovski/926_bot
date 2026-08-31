"""Owner request 2026-08-31: plan corrections must REACH Telegram.

The auto-plan summary is recomputed every five minutes and, on a material
change, silently edited in place. That kept the picture true but told the
owner nothing — a correction was only visible to someone who scrolled back
to the morning message and re-read it. Now the pairs that actually moved
also get one message of their own, naming what moved, throttled to once an
hour per pair so a five-minute recompute cannot turn quiet mode inside out.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.db import Database
from app.services.smc.models import Direction, Trend
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.planbook import (
    describe_plan_changes,
    plan_fingerprint,
    plan_snapshot,
)
from app.services.smc.state import WatcherState

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _scenario(direction=Direction.LONG, bottom=2400.0, top=2410.0, spec=False):
    return PlanScenario(
        direction=direction,
        zone_bottom=bottom,
        zone_top=top,
        entry=bottom,
        stop_loss=bottom - 10,
        take_profit=top + 40,
        rr=3.0,
        speculative=spec,
    )


def _plan(scenarios=None, blocker=None, price=2456.0):
    return PairPlan(
        pair="ETHUSD",
        price=price,
        price_decimals=2,
        h4_trend=Trend.UP,
        scenarios=scenarios if scenarios is not None else [_scenario()],
        blocker=blocker,
    )


class TestSnapshot:
    def test_it_carries_the_same_facts_as_the_fingerprint(self):
        plan = _plan(blocker="price has not reached the zone yet")
        snap = plan_snapshot(plan)
        assert snap["blocker"] == "price has not reached the zone yet"
        assert snap["scenarios"] == [["long", 2400.0, 2410.0, False]]

    def test_price_drift_is_not_material(self):
        a, b = _plan(price=2456.0), _plan(price=2499.0)
        assert plan_snapshot(a) == plan_snapshot(b)
        assert plan_fingerprint(a) == plan_fingerprint(b)

    def test_it_is_json_safe(self):
        import json

        json.dumps(plan_snapshot(_plan()))  # rides in the kv store


class TestDescribeChanges:
    def test_no_change_says_nothing(self):
        snap = plan_snapshot(_plan())
        assert describe_plan_changes(snap, snap) == []

    def test_a_moved_zone_reads_as_a_move(self):
        old = plan_snapshot(_plan([_scenario()]))
        new = plan_snapshot(_plan([_scenario(bottom=2415.0, top=2425.0)]))
        assert describe_plan_changes(old, new) == [
            "LONG zone moved 2400.00–2410.00 → 2415.00–2425.00"
        ]

    def test_an_added_side(self):
        old = plan_snapshot(_plan([_scenario()]))
        new = plan_snapshot(
            _plan([_scenario(), _scenario(Direction.SHORT, 2500.0, 2510.0)])
        )
        assert describe_plan_changes(old, new) == [
            "SHORT scenario added: 2500.00–2510.00"
        ]

    def test_a_dropped_side(self):
        old = plan_snapshot(
            _plan([_scenario(), _scenario(Direction.SHORT, 2500.0, 2510.0)])
        )
        new = plan_snapshot(_plan([_scenario()]))
        assert describe_plan_changes(old, new) == [
            "SHORT scenario dropped (2500.00–2510.00)"
        ]

    def test_a_speculative_flip_is_named(self):
        old = plan_snapshot(_plan([_scenario(spec=True)]))
        new = plan_snapshot(_plan([_scenario(bottom=2401.0, spec=False)]))
        assert describe_plan_changes(old, new)[-1] == "LONG is now confirmed"

    def test_blocker_transitions(self):
        live = plan_snapshot(_plan(blocker=None))
        blocked = plan_snapshot(_plan(blocker="H1 has no valid untested zone"))
        assert describe_plan_changes(live, blocked) == [
            "Now waiting: H1 has no valid untested zone"
        ]
        assert describe_plan_changes(blocked, live) == [
            "Blocker cleared — the plan is live"
        ]

    def test_decimals_follow_the_instrument(self):
        old = plan_snapshot(_plan([_scenario(bottom=1.1000, top=1.1050)]))
        new = plan_snapshot(_plan([_scenario(bottom=1.1100, top=1.1150)]))
        assert "1.10000–1.10500 → 1.11000–1.11500" in describe_plan_changes(
            old, new, decimals=5
        )[0]


class _FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)
        return len(self.sent)


def _watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    watcher.notifier = _FakeNotifier()
    return watcher


class TestNotification:
    @pytest.mark.asyncio
    async def test_a_moved_plan_is_announced(self, tmp_path):
        watcher = _watcher(tmp_path)
        before = plan_snapshot(_plan([_scenario()]))
        after = plan_snapshot(_plan([_scenario(bottom=2415.0, top=2425.0)]))
        await watcher._notify_plan_changes(
            {"ETHUSD": "old"}, {"ETHUSD": "new"},
            {"ETHUSD": before}, {"ETHUSD": after},
        )
        assert len(watcher.notifier.sent) == 1
        text = watcher.notifier.sent[0]
        assert "Plan updated — ETHUSD" in text
        assert "LONG zone moved 2400.00–2410.00 → 2415.00–2425.00" in text

    @pytest.mark.asyncio
    async def test_an_unchanged_pair_stays_silent(self, tmp_path):
        watcher = _watcher(tmp_path)
        snap = plan_snapshot(_plan())
        await watcher._notify_plan_changes(
            {"ETHUSD": "same"}, {"ETHUSD": "same"},
            {"ETHUSD": snap}, {"ETHUSD": snap},
        )
        assert watcher.notifier.sent == []

    @pytest.mark.asyncio
    async def test_the_second_change_within_the_hour_is_throttled(self, tmp_path):
        watcher = _watcher(tmp_path)
        a = plan_snapshot(_plan([_scenario()]))
        b = plan_snapshot(_plan([_scenario(bottom=2415.0, top=2425.0)]))
        c = plan_snapshot(_plan([_scenario(bottom=2430.0, top=2440.0)]))
        await watcher._notify_plan_changes(
            {"ETHUSD": "1"}, {"ETHUSD": "2"}, {"ETHUSD": a}, {"ETHUSD": b}
        )
        await watcher._notify_plan_changes(
            {"ETHUSD": "2"}, {"ETHUSD": "3"}, {"ETHUSD": b}, {"ETHUSD": c}
        )
        assert len(watcher.notifier.sent) == 1, "the recompute must not spam"

    @pytest.mark.asyncio
    async def test_it_speaks_again_once_the_cooldown_passes(self, tmp_path):
        watcher = _watcher(tmp_path)
        a = plan_snapshot(_plan([_scenario()]))
        b = plan_snapshot(_plan([_scenario(bottom=2415.0, top=2425.0)]))
        watcher.state.plan_change_notified["ETHUSD"] = (
            datetime.now(tz=timezone.utc) - timedelta(hours=2)
        ).isoformat()
        await watcher._notify_plan_changes(
            {"ETHUSD": "1"}, {"ETHUSD": "2"}, {"ETHUSD": a}, {"ETHUSD": b}
        )
        assert len(watcher.notifier.sent) == 1

    @pytest.mark.asyncio
    async def test_a_poisoned_timestamp_does_not_mute_forever(self, tmp_path):
        watcher = _watcher(tmp_path)
        watcher.state.plan_change_notified["ETHUSD"] = "not-a-timestamp"
        a = plan_snapshot(_plan([_scenario()]))
        b = plan_snapshot(_plan([_scenario(bottom=2415.0, top=2425.0)]))
        await watcher._notify_plan_changes(
            {"ETHUSD": "1"}, {"ETHUSD": "2"}, {"ETHUSD": a}, {"ETHUSD": b}
        )
        assert len(watcher.notifier.sent) == 1

    @pytest.mark.asyncio
    async def test_no_stored_snapshot_records_nothing_and_says_nothing(
        self, tmp_path
    ):
        """First cycle after this shipped: there is nothing honest to diff."""
        watcher = _watcher(tmp_path)
        after = plan_snapshot(_plan())
        await watcher._notify_plan_changes(
            {"ETHUSD": "old"}, {"ETHUSD": "new"}, {}, {"ETHUSD": after}
        )
        assert watcher.notifier.sent == []
        assert "ETHUSD" not in watcher.state.plan_change_notified

    @pytest.mark.asyncio
    async def test_the_timestamp_is_only_recorded_on_a_successful_send(
        self, tmp_path
    ):
        watcher = _watcher(tmp_path)

        class _Dead:
            sent: list = []

            async def send(self, text, **kwargs):
                return None

        watcher.notifier = _Dead()
        a = plan_snapshot(_plan([_scenario()]))
        b = plan_snapshot(_plan([_scenario(bottom=2415.0, top=2425.0)]))
        await watcher._notify_plan_changes(
            {"ETHUSD": "1"}, {"ETHUSD": "2"}, {"ETHUSD": a}, {"ETHUSD": b}
        )
        assert "ETHUSD" not in watcher.state.plan_change_notified
