"""Tests for /plan⇄watcher info merge: engine in_zone flag and the
live-status line shown in /plan. The zone-touch ping tests moved to
test_autoplan.py::TestPlanZoneAlert (plan-centric zone alert)."""

from datetime import datetime, timezone

from app.core.config import settings
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Verdict
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
    return TripleSyncEngine(max_entry_gap_r=99.0).evaluate(
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
        self.pair_profile = {}

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


class TestLiveStatus:
    def test_reports_live_setup(self, monkeypatch):
        w = _watcher(monkeypatch)
        # Rule 5.1's default gate (0.5R) is unrelated to what this test
        # covers and m5_long_trigger's live price sits further than that
        # from its entry; disable it so _live_status keeps reporting the
        # setup. Only this test runs the engine through _build_engine, so
        # only it needs the override.
        monkeypatch.setattr(settings.smc, "max_entry_gap_r", 99.0)
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

    def test_reports_live_setup_without_a_take_profit(self, monkeypatch):
        """Detector mode: with no unswept liquidity ahead the setup has
        take_profit=None and rr 0.0. The /plan live line must still render —
        an unformattable None here costs the owner the whole message."""
        import app.services.smc.engine as engine_mod

        w = _watcher(monkeypatch)
        monkeypatch.setattr(settings.smc, "max_entry_gap_r", 99.0)
        monkeypatch.setattr(engine_mod, "nearest_liquidity", lambda *a, **k: None)
        monkeypatch.setattr(engine_mod, "liquidity_ladder", lambda *a, **k: [])
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
        assert "no TP (no structural objective)" in line
        assert "RR" not in line

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
