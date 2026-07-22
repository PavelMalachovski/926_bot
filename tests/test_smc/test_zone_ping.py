"""Tests for /plan⇄watcher info merge: engine in_zone flag, zone-touch ping,
and the live-status line shown in /plan."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Verdict, Zone
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)


def _fresh():
    return AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )


def _eval(m5, h4=None):
    return TripleSyncEngine(min_rr=2.0).evaluate(
        h4=h4 or make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5=m5,
        result=_fresh(),
    )


class TestEngineInZone:
    def test_in_zone_true_when_waiting_for_choch(self):
        res = _eval(m5_long_trigger()[:16])  # in the zone, no CHoCH yet
        assert res.verdict == Verdict.WATCH and res.in_zone

    def test_in_zone_true_when_approved(self):
        res = _eval(m5_long_trigger())
        assert res.verdict == Verdict.APPROVED_LIMIT and res.in_zone

    def test_in_zone_false_when_price_not_reached(self):
        m5 = make_candles([3180, 3178, 3176, 3175, 3174, 3175, 3176, 3175, 3174])
        res = _eval(m5)
        assert res.verdict == Verdict.WATCH and not res.in_zone

    def test_in_zone_false_when_flat(self):
        flat = [3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000]
        res = _eval(m5_long_trigger(), h4=make_candles(flat, step_minutes=240))
        assert not res.in_zone


class _State:
    def __init__(self):
        self.zone_pinged = {}
        self.pair_cooldown = {}

    def save(self):
        pass


class _Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kwargs):
        self.sent.append(text)
        return 1


def _watcher(monkeypatch):
    from smc_watcher import Watcher

    monkeypatch.setattr(settings.smc, "zone_ping", True)
    w = Watcher.__new__(Watcher)
    w.state = _State()
    w.notifier = _Notifier()
    return w


def _in_zone_result():
    r = AnalysisResult(
        symbol="USDJPY",
        verdict=Verdict.WATCH,
        checked_at=datetime.now(tz=timezone.utc),
        price_decimals=3,
    )
    r.in_zone = True
    r.h1_zone = Zone(
        bottom=162.0, top=162.12, is_demand=True, pivot_index=0,
        timestamp=r.checked_at,
    )
    return r


class TestZonePing:
    @pytest.mark.asyncio
    async def test_first_entry_pings_once(self, monkeypatch):
        w = _watcher(monkeypatch)
        r = _in_zone_result()
        await w._maybe_zone_ping("USDJPY", r)
        await w._maybe_zone_ping("USDJPY", r)  # still in zone -> no second ping
        assert len(w.notifier.sent) == 1
        assert "reached the H1 Demand zone" in w.notifier.sent[0]

    @pytest.mark.asyncio
    async def test_reset_on_leaving_then_repings(self, monkeypatch):
        w = _watcher(monkeypatch)
        await w._maybe_zone_ping("USDJPY", _in_zone_result())
        left = _in_zone_result()
        left.in_zone = False
        await w._maybe_zone_ping("USDJPY", left)  # left the zone -> reset
        await w._maybe_zone_ping("USDJPY", _in_zone_result())  # re-entry pings
        assert len(w.notifier.sent) == 2

    @pytest.mark.asyncio
    async def test_no_ping_when_approved(self, monkeypatch):
        w = _watcher(monkeypatch)
        r = _in_zone_result()
        r.verdict = Verdict.APPROVED_LIMIT  # full alert handles this, not a ping
        await w._maybe_zone_ping("USDJPY", r)
        assert w.notifier.sent == []

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_ping(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.state.pair_cooldown["USDJPY"] = (
            datetime.now(tz=timezone.utc) + timedelta(hours=2)
        ).isoformat()
        await w._maybe_zone_ping("USDJPY", _in_zone_result())
        assert w.notifier.sent == []

    @pytest.mark.asyncio
    async def test_disabled_by_flag(self, monkeypatch):
        w = _watcher(monkeypatch)
        monkeypatch.setattr(settings.smc, "zone_ping", False)
        await w._maybe_zone_ping("USDJPY", _in_zone_result())
        assert w.notifier.sent == []


class TestLiveStatus:
    def test_reports_live_setup(self, monkeypatch):
        w = _watcher(monkeypatch)
        data = {
            "h4": make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            "h1": make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            "m5": m5_long_trigger(),
        }
        line = w._live_status(
            get_instrument("ETHUSD"),
            data,
            datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
        )
        assert "LIVE SETUP NOW" in line

    def test_reports_watch_reason(self, monkeypatch):
        w = _watcher(monkeypatch)
        data = {
            "h4": make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            "h1": make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            "m5": m5_long_trigger()[:16],
        }
        line = w._live_status(
            get_instrument("ETHUSD"),
            data,
            datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
        )
        assert "👀" in line and "zone" in line.lower()
