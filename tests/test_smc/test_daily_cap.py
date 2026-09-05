"""D25 (owner decision 2026-09-05): one, at most two setup alerts per pair per
Prague trading day. A regular setup past the cap is journal-recorded and
dedup-fingerprinted but NOT sent; a ⭐ setup always goes through; the counter
reads zero on a new day; 0 disables the cap."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.smc.db import Database
from app.services.smc.journal import SignalJournal
from app.services.smc.models import (
    FVG,
    AnalysisResult,
    Direction,
    TradeSetup,
    Verdict,
    Zone,
)
from app.services.smc.planbook import PlanBook
from app.services.smc.state import WatcherState

DAY = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)  # Monday, London block


def _approved(zone_bottom, star=False, when=DAY):
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT, checked_at=when,
        price=zone_bottom + 30.0,
    )
    result.session_name = "Frankfurt/London"
    result.h1_zone = Zone(
        bottom=zone_bottom, top=zone_bottom + 7.0, is_demand=True,
        pivot_index=3, timestamp=when,
    )
    entry = zone_bottom + 10.0
    result.setup = TradeSetup(
        direction=Direction.LONG, entry=entry, stop_loss=entry - 10.0,
        take_profit=entry + 20.0, rr=2.0,
        fvg=FVG(9, entry - 2.0, entry, True, when),
        tier_star=star, tier_missed=[] if star else ["room"],
    )
    return result


class _Notifier:
    def __init__(self):
        self.sent = []
        self.pinned = []

    async def send(self, text, reply_markup=None, disable_notification=False):
        self.sent.append(text)
        return len(self.sent)

    async def send_photo(self, photo, caption=None, reply_to=None):
        return 99

    async def pin(self, message_id):
        self.pinned.append(message_id)


def _watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    w = Watcher.__new__(Watcher)
    w.db = db
    w.state = WatcherState(db)
    w.state.pairs = ["ETHUSD"]
    w.journal = SignalJournal(db)
    w.notifier = _Notifier()
    w.news = None
    w.last_results = {}
    w.planbook = PlanBook()

    async def _no_chart(*a, **k):
        return None

    async def _no_track(*a, **k):
        return None

    w._send_chart = _no_chart
    w._track_journal = _no_track
    return w


@pytest.fixture(autouse=True)
def _cap_two(monkeypatch):
    monkeypatch.setattr(settings.smc, "max_setups_per_day", 2)


class TestDailyCap:
    @pytest.mark.asyncio
    async def test_third_regular_setup_is_recorded_but_not_sent(self, tmp_path):
        w = _watcher(tmp_path)
        outcomes = []
        for i, bottom in enumerate((3000.0, 3100.0, 3200.0)):
            outcomes.append(await w._send_alert("ETHUSD", _approved(bottom), f"fp{i}"))

        assert outcomes == [True, True, False]
        assert len(w.notifier.sent) == 2
        assert len(w.journal.signals) == 3  # detector mode: every setup is recorded
        assert w.state.last_setup["ETHUSD"] == "fp2"  # ... and dedup-fingerprinted
        assert w.state.daily_count("setup", "ETHUSD", DAY) == 2

    @pytest.mark.asyncio
    async def test_a_star_setup_always_goes_through(self, tmp_path):
        w = _watcher(tmp_path)
        for i, bottom in enumerate((3000.0, 3100.0)):
            await w._send_alert("ETHUSD", _approved(bottom), f"fp{i}")
        assert await w._send_alert("ETHUSD", _approved(3200.0, star=True), "fp-star")
        assert len(w.notifier.sent) == 3
        assert w.notifier.pinned == [3]  # the ⭐ is the one that gets pinned
        # ... and it spends allowance too: the day's count is honest
        assert w.state.daily_count("setup", "ETHUSD", DAY) == 3

    @pytest.mark.asyncio
    async def test_the_cap_is_per_pair(self, tmp_path):
        w = _watcher(tmp_path)
        for i, bottom in enumerate((3000.0, 3100.0)):
            await w._send_alert("ETHUSD", _approved(bottom), f"fp{i}")
        other = _approved(150.0)
        other.symbol = "USDJPY"
        assert await w._send_alert("USDJPY", other, "fp-jpy") is True

    @pytest.mark.asyncio
    async def test_a_new_day_starts_at_zero(self, tmp_path):
        w = _watcher(tmp_path)
        for i, bottom in enumerate((3000.0, 3100.0)):
            await w._send_alert("ETHUSD", _approved(bottom), f"fp{i}")
        tomorrow = DAY + timedelta(days=1)
        assert await w._send_alert(
            "ETHUSD", _approved(3200.0, when=tomorrow), "fp-next"
        ) is True
        assert w.state.daily_count("setup", "ETHUSD", tomorrow) == 1

    @pytest.mark.asyncio
    async def test_zero_disables_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.smc, "max_setups_per_day", 0)
        w = _watcher(tmp_path)
        for i, bottom in enumerate((3000.0, 3100.0, 3200.0, 3300.0)):
            assert await w._send_alert("ETHUSD", _approved(bottom), f"fp{i}")
        assert len(w.notifier.sent) == 4

    def test_counter_survives_a_restart(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        state = WatcherState(db)
        state.bump_daily("setup", "ETHUSD", DAY)
        state.bump_daily("setup", "ETHUSD", DAY)
        assert WatcherState(Database(str(tmp_path / "smc.db"))).daily_count(
            "setup", "ETHUSD", DAY
        ) == 2

    def test_poisoned_counter_reads_as_zero(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        db.kv_set("daily_counts", {"setup": {"ETHUSD": ["2026-09-07", "lots"]}, "x": 3})
        state = WatcherState(db)
        assert state.daily_count("setup", "ETHUSD", DAY) == 0

    @pytest.mark.asyncio
    async def test_heartbeat_names_the_cap_not_a_failure(self, tmp_path, monkeypatch):
        """A capped setup must read as a deliberate limit in the cycle
        summary — never as 'the alert failed to send'."""
        w = _watcher(tmp_path)
        w.state.bump_daily("setup", "ETHUSD", DAY)
        w.state.bump_daily("setup", "ETHUSD", DAY)

        async def fake_check_pair(key):
            return "line", _approved(3200.0)

        monkeypatch.setattr(w, "check_pair", fake_check_pair)
        summary = await w.run_cycle()
        assert "daily limit of 2 reached" in summary
        assert "failed to send" not in summary
        assert w.notifier.sent == []
