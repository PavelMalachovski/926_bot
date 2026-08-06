"""Tests for instruments registry, OANDA parsing, state and correlation guard."""

import json
from datetime import datetime, timezone

import pytest

from app.services.smc.instruments import DEFAULT_PAIRS, INSTRUMENTS, get_instrument
from app.services.smc.oanda import _parse_time
from app.services.smc.state import WatcherState


class TestInstruments:
    def test_registry_covers_strategy_universe(self):
        assert set(INSTRUMENTS) == {"ETHUSD", "USDJPY", "EURUSD", "GBPUSD", "USDCAD"}
        assert DEFAULT_PAIRS == ["ETHUSD", "USDJPY"]

    def test_fvg_minimums_follow_rule_4(self):
        assert get_instrument("ETHUSD").min_fvg == 2.0  # $2
        assert get_instrument("USDJPY").min_fvg == 0.050  # 5 pips of 0.01
        assert get_instrument("EURUSD").min_fvg == 0.00050  # 5 pips of 0.0001

    def test_only_crypto_checks_funding(self):
        assert get_instrument("ethusd").check_funding
        assert not get_instrument("USDJPY").check_funding

    def test_sources(self):
        assert get_instrument("ETHUSD").source == "crypto"
        assert all(
            get_instrument(k).source == "forex"
            for k in ("USDJPY", "EURUSD", "GBPUSD", "USDCAD")
        )

    def test_forex_uses_yahoo_without_oanda_token(self, monkeypatch):
        from smc_watcher import _build_fetcher
        from app.core.config import settings
        from app.services.smc.yahoo import YahooDataFetcher
        from app.services.smc.oanda import OandaDataFetcher

        monkeypatch.setattr(settings.smc, "forex_source", "auto")
        monkeypatch.setattr(settings.oanda, "api_token", None)
        monkeypatch.setattr(settings.twelvedata, "api_key", None)
        fetcher = _build_fetcher(get_instrument("USDJPY"))
        assert isinstance(fetcher, YahooDataFetcher)
        assert fetcher.symbol == "USDJPY=X"

        monkeypatch.setattr(settings.oanda, "api_token", "tok")
        fetcher = _build_fetcher(get_instrument("USDJPY"))
        assert isinstance(fetcher, OandaDataFetcher)


class TestOandaTimeParsing:
    def test_nanosecond_timestamp(self):
        dt = _parse_time("2026-07-06T14:00:00.000000000Z")
        assert (dt.year, dt.hour, dt.minute) == (2026, 14, 0)
        assert dt.tzinfo is not None

    def test_plain_timestamp(self):
        dt = _parse_time("2026-07-06T14:05:00Z")
        assert dt.minute == 5


class TestWatcherState:
    @staticmethod
    def _db(tmp_path):
        from app.services.smc.db import Database

        return Database(str(tmp_path / "smc.db"))

    def test_defaults_when_no_data(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        assert state.pairs == ["ETHUSD", "USDJPY"]

    def test_toggle_and_persist(self, tmp_path):
        db = self._db(tmp_path)
        state = WatcherState(db)
        assert state.toggle_pair("EURUSD") is True
        assert state.toggle_pair("USDJPY") is False
        assert state.pairs == ["ETHUSD", "EURUSD"]

        reloaded = WatcherState(db)
        assert reloaded.pairs == ["ETHUSD", "EURUSD"]

    def test_unknown_pairs_in_db_are_dropped(self, tmp_path):
        db = self._db(tmp_path)
        db.kv_set("pairs", ["ETHUSD", "DOGEUSD"])
        state = WatcherState(db)
        assert state.pairs == ["ETHUSD"]

    def test_legacy_json_migration(self, tmp_path):
        from app.services.smc.db import Database, migrate_legacy_json

        state_file = tmp_path / "state.json"
        journal_file = tmp_path / "journal.json"
        state_file.write_text(
            json.dumps({"pairs": ["ETHUSD", "GBPUSD"], "last_setup": {"ETHUSD": "x"}})
        )
        journal_file.write_text(
            json.dumps(
                [
                    {
                        "id": "legacy1",
                        "pair": "ETHUSD",
                        "direction": "long",
                        "entry": 100.0,
                        "stop_loss": 95.0,
                        "take_profit": 110.0,
                        "rr": 2.0,
                        "session": "New York",
                        "created_at": "2026-07-15T14:00:00+00:00",
                        "expires_at": None,
                        "status": "tp",
                        "filled_at": None,
                        "resolved_at": None,
                        "checked_until": None,
                    }
                ]
            )
        )
        db = Database(str(tmp_path / "smc.db"))
        migrate_legacy_json(db, str(state_file), str(journal_file))
        assert db.kv_get("pairs") == ["ETHUSD", "GBPUSD"]
        assert db.signals_all()[0]["id"] == "legacy1"
        assert not state_file.exists()  # renamed to .bak
        assert (tmp_path / "state.json.bak").exists()


class TestPlanZones:
    """Zones a /plan run showed are remembered for the Prague day, so an
    alert can say whether it came from the morning picture."""

    @staticmethod
    def _db(tmp_path):
        from app.services.smc.db import Database

        return Database(str(tmp_path / "smc.db"))

    def test_zones_are_remembered_and_matched_by_overlap(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        state.remember_plan_zones("ETHUSD", [(3131.0, 3138.0)])
        assert state.zone_was_planned("ETHUSD", 3135.0, 3142.0) is True
        assert state.zone_was_planned("ETHUSD", 3200.0, 3210.0) is False

    def test_a_zone_that_shifted_by_a_tick_is_still_the_same_idea(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        state.remember_plan_zones("ETHUSD", [(3131.0, 3138.0)])
        # an H1 zone drifts as new pivots confirm — still the same zone
        assert state.zone_was_planned("ETHUSD", 3131.01, 3138.01) is True
        assert state.zone_was_planned("ETHUSD", 3130.99, 3137.99) is True
        # touching edges still overlap
        assert state.zone_was_planned("ETHUSD", 3138.0, 3145.0) is True
        # a zone that genuinely moved does not
        assert state.zone_was_planned("ETHUSD", 3138.01, 3145.0) is False

    def test_direction_separates_zones_at_the_same_price(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        state.remember_plan_zones("ETHUSD", [(3131.0, 3138.0, "long")])
        assert state.zone_was_planned("ETHUSD", 3132.0, 3139.0, "long") is True
        assert state.zone_was_planned("ETHUSD", 3132.0, 3139.0, "short") is False

    def test_zones_survive_a_restart(self, tmp_path):
        from app.services.smc.db import Database

        WatcherState(self._db(tmp_path)).remember_plan_zones(
            "ETHUSD", [(3131.0, 3138.0)]
        )
        reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
        assert reloaded.has_plan_today("ETHUSD") is True
        assert reloaded.zone_was_planned("ETHUSD", 3135.0, 3140.0) is True

    def test_a_new_prague_day_replaces_the_previous_set(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        day1 = datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 8, 6, 6, 0, tzinfo=timezone.utc)
        state.remember_plan_zones("ETHUSD", [(3131.0, 3138.0)], now=day1)
        assert state.zone_was_planned("ETHUSD", 3135.0, 3140.0, now=day1) is True
        state.remember_plan_zones("USDJPY", [(150.0, 151.0)], now=day2)
        assert state.has_plan_today("ETHUSD", now=day2) is False
        assert state.zone_was_planned("ETHUSD", 3135.0, 3140.0, now=day2) is False
        assert state.has_plan_today("USDJPY", now=day2) is True

    def test_no_plan_today_is_distinct_from_a_zone_not_in_the_plan(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        assert state.has_plan_today("ETHUSD") is False
        state.remember_plan_zones("ETHUSD", [(3131.0, 3138.0)])
        assert state.has_plan_today("ETHUSD") is True
        assert state.zone_was_planned("ETHUSD", 3300.0, 3310.0) is False

    def test_a_plan_with_no_scenarios_still_counts_as_a_plan(self, tmp_path):
        state = WatcherState(self._db(tmp_path))
        state.remember_plan_zones("ETHUSD", [])
        assert state.has_plan_today("ETHUSD") is True
        assert state.zone_was_planned("ETHUSD", 3131.0, 3138.0) is False


def _fingerprint_result(
    zone=(3131.0, 3138.0), entry=3140.5, session="Frankfurt/London",
    direction="long", when=None,
):
    from app.services.smc.models import (
        AnalysisResult,
        Direction,
        FVG,
        TradeSetup,
        Verdict,
        Zone,
    )

    checked_at = when or datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT, checked_at=checked_at,
    )
    result.session_name = session
    result.h1_zone = Zone(
        bottom=zone[0], top=zone[1], is_demand=direction == "long",
        pivot_index=3, timestamp=checked_at,
    )
    result.setup = TradeSetup(
        direction=Direction(direction),
        entry=entry,
        stop_loss=entry - 10,
        take_profit=entry + 20,
        rr=2.0,
        fvg=FVG(9, entry - 2, entry, True, checked_at),
    )
    return result


class TestSetupFingerprint:
    """Detector mode (spec §3): one announcement per zone per session block —
    a second imbalance in the same zone is the same trading idea."""

    def test_two_imbalances_in_one_zone_collapse_to_one_key(self):
        from smc_watcher import _setup_fingerprint

        a = _fingerprint_result(zone=(3131.0, 3138.0), entry=3140.5)
        b = _fingerprint_result(zone=(3131.0, 3138.0), entry=3139.0)
        assert _setup_fingerprint(a) == _setup_fingerprint(b)

    def test_the_same_zone_in_the_next_session_block_is_a_new_key(self):
        from smc_watcher import _setup_fingerprint

        london = _fingerprint_result(session="Frankfurt/London")
        newyork = _fingerprint_result(session="New York")
        assert _setup_fingerprint(london) != _setup_fingerprint(newyork)

    def test_a_different_zone_is_a_new_key(self):
        from smc_watcher import _setup_fingerprint

        a = _fingerprint_result(zone=(3131.0, 3138.0), entry=3140.5)
        b = _fingerprint_result(zone=(3200.0, 3208.0), entry=3140.5)
        assert _setup_fingerprint(a) != _setup_fingerprint(b)

    def test_the_opposite_direction_is_a_new_key(self):
        from smc_watcher import _setup_fingerprint

        a = _fingerprint_result(direction="long")
        b = _fingerprint_result(direction="short")
        assert _setup_fingerprint(a) != _setup_fingerprint(b)


class TestAlertPlanProvenance:
    """The alert says whether its zone was in the morning plan — and says
    nothing at all when no /plan was run today."""

    class _FakeNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            self.sent.append(text)
            return 42

        async def pin(self, message_id):
            pass

    def _watcher(self, tmp_path):
        from app.services.smc.db import Database
        from app.services.smc.journal import SignalJournal
        from smc_watcher import Watcher

        async def _no_chart(*args, **kwargs):
            return None

        db = Database(str(tmp_path / "smc.db"))
        watcher = Watcher.__new__(Watcher)
        watcher.db = db
        watcher.state = WatcherState(db)
        watcher.journal = SignalJournal(db)
        watcher.notifier = self._FakeNotifier()
        watcher._send_chart = _no_chart
        return watcher

    @staticmethod
    def _approved():
        from app.services.smc.engine import TripleSyncEngine
        from app.services.smc.models import AnalysisResult, Verdict
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

    @pytest.mark.asyncio
    async def test_no_plan_today_says_nothing_about_the_plan(self, tmp_path):
        watcher = self._watcher(tmp_path)
        result = self._approved()
        await watcher._send_alert("ETHUSD", result, "fp")
        text = watcher.notifier.sent[0]
        assert "from this morning's plan" not in text
        assert "not in the plan" not in text

    @pytest.mark.asyncio
    async def test_zone_from_the_morning_plan_is_labelled(self, tmp_path):
        watcher = self._watcher(tmp_path)
        result = self._approved()
        zone = result.h1_zone
        watcher.state.remember_plan_zones(
            "ETHUSD", [(zone.bottom, zone.top, "long")]
        )
        await watcher._send_alert("ETHUSD", result, "fp")
        assert "from this morning's plan" in watcher.notifier.sent[0]

    @pytest.mark.asyncio
    async def test_a_zone_outside_the_plan_is_labelled_new(self, tmp_path):
        watcher = self._watcher(tmp_path)
        result = self._approved()
        watcher.state.remember_plan_zones("ETHUSD", [(1.0, 2.0, "long")])
        await watcher._send_alert("ETHUSD", result, "fp")
        assert "new zone — not in the plan" in watcher.notifier.sent[0]


class TestCorrelationGuard:
    @staticmethod
    def _approved(symbol, direction):
        from datetime import datetime, timezone

        from app.services.smc.models import (
            AnalysisResult,
            Direction,
            FVG,
            TradeSetup,
            Verdict,
        )

        result = AnalysisResult(
            symbol=symbol,
            verdict=Verdict.APPROVED_LIMIT,
            checked_at=datetime.now(tz=timezone.utc),
        )
        result.setup = TradeSetup(
            direction=Direction(direction),
            entry=1.0,
            stop_loss=0.9,
            take_profit=1.2,
            rr=2.0,
            fvg=FVG(0, 0.95, 1.0, True, result.checked_at),
        )
        return result

    def test_eur_gbp_same_direction_forbidden(self):
        from smc_watcher import _correlation_warnings

        warnings = _correlation_warnings(
            [self._approved("EURUSD", "long"), self._approved("GBPUSD", "long")]
        )
        assert any("EURUSD and GBPUSD" in w for w in warnings)

    def test_triple_usd_bet_forbidden(self):
        from smc_watcher import _correlation_warnings

        warnings = _correlation_warnings(
            [self._approved("GBPUSD", "long"), self._approved("USDJPY", "short")]
        )
        assert any("triple bet" in w for w in warnings)

    def test_allowed_combination_is_silent(self):
        from smc_watcher import _correlation_warnings

        warnings = _correlation_warnings(
            [self._approved("ETHUSD", "long"), self._approved("USDJPY", "long")]
        )
        assert warnings == []


class TestMorningDigestSkipsWeekends:
    """Forex Factory has no Saturday/Sunday releases — the 07:45 digest must
    stay silent then instead of sending an empty 'no red news' message."""

    class _FakeState:
        def __init__(self):
            self.last_digest_date = ""
            self.pairs = ["ETHUSD", "USDJPY"]

        def save(self):
            pass

    class _FakeNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            self.sent.append(text)
            return 1

    def _watcher_stub(self, monkeypatch):
        from smc_watcher import Watcher
        from app.services.smc.news import NewsCalendar

        stub = Watcher.__new__(Watcher)
        stub.state = self._FakeState()
        stub.notifier = self._FakeNotifier()
        stub.news = NewsCalendar()
        stub.news.fetched_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        return stub

    def _freeze(self, monkeypatch, when):
        import smc_watcher as sw

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return when

        monkeypatch.setattr(sw, "datetime", _Frozen)

    def test_no_digest_on_saturday(self, monkeypatch):
        import asyncio

        # 2026-07-18 07:00 UTC = Saturday 09:00 Prague — well past digest_after
        self._freeze(monkeypatch, datetime(2026, 7, 18, 7, 0, tzinfo=timezone.utc))
        stub = self._watcher_stub(monkeypatch)
        asyncio.run(stub._morning_briefing())
        assert stub.notifier.sent == []
        assert stub.state.last_digest_date == ""

    def test_no_digest_on_sunday(self, monkeypatch):
        import asyncio

        # 2026-07-19 07:00 UTC = Sunday 09:00 Prague
        self._freeze(monkeypatch, datetime(2026, 7, 19, 7, 0, tzinfo=timezone.utc))
        stub = self._watcher_stub(monkeypatch)
        asyncio.run(stub._morning_briefing())
        assert stub.notifier.sent == []

    def test_digest_still_sent_on_weekday(self, monkeypatch):
        import asyncio

        # 2026-07-16 07:00 UTC = Thursday 09:00 Prague
        self._freeze(monkeypatch, datetime(2026, 7, 16, 7, 0, tzinfo=timezone.utc))
        stub = self._watcher_stub(monkeypatch)
        asyncio.run(stub._morning_briefing())
        assert len(stub.notifier.sent) == 1
        assert stub.state.last_digest_date == "2026-07-16"
