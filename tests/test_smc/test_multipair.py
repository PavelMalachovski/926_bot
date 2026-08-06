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

    def test_forex_requires_a_configured_key(self, monkeypatch):
        """No keyless fallback any more: with neither key set, resolving a
        forex fetcher must fail clearly rather than silently degrade."""
        from smc_watcher import _build_fetcher
        from app.core.config import settings
        from app.core.exceptions import ConfigurationError
        from app.services.smc.oanda import OandaDataFetcher

        monkeypatch.setattr(settings.smc, "forex_source", "auto")
        monkeypatch.setattr(settings.oanda, "api_token", None)
        monkeypatch.setattr(settings.twelvedata, "api_key", None)
        with pytest.raises(ConfigurationError):
            _build_fetcher(get_instrument("USDJPY"))

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
    async def test_a_zone_named_only_by_a_blocker_is_still_the_morning_plan(
        self, tmp_path
    ):
        """The plan filters on min_rr, the alert does not: the zone whose RR
        made it a plan blocker is exactly the one detector mode announces.
        The owner read its bounds this morning (owner decision 2026-08-06)."""
        from app.services.smc.instruments import get_instrument
        from app.services.smc.plan import build_plan
        from tests.test_smc.helpers import (
            H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, make_candles,
        )

        watcher = self._watcher(tmp_path)
        plan = build_plan(
            get_instrument("ETHUSD"),
            make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            make_candles([3160.0], step_minutes=5),
            min_rr=99.0,  # nothing survives the plan's own filter
        )
        assert plan.scenarios == [] and plan.blocker_zone
        watcher.state.remember_plan_zones("ETHUSD", plan.zones_shown())
        await watcher._send_alert("ETHUSD", self._approved(), "fp")
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


class TestAlertSendIsolation:
    """A failure sending one pair's alert (most plausibly `format_result`
    raising) must not stop the rest of `run_cycle`'s `for key in ...` loop —
    the same per-pair isolation `check_pair()` and `_track_journal()`'s own
    loop already give every other per-pair operation there (coordinator
    review, 2026-08-06: this used to propagate out of the loop and silently
    drop every remaining pair's check for the cycle)."""

    class _FakeNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            self.sent.append(text)
            return 42

        async def pin(self, message_id):
            pass

    @staticmethod
    def _approved(symbol):
        from app.services.smc.models import (
            AnalysisResult,
            Direction,
            FVG,
            TradeSetup,
            Verdict,
        )

        result = AnalysisResult(
            symbol=symbol, verdict=Verdict.APPROVED_LIMIT,
            checked_at=datetime.now(tz=timezone.utc),
        )
        result.session_name = "New York"
        result.setup = TradeSetup(
            direction=Direction.LONG, entry=1.0, stop_loss=0.9,
            take_profit=1.2, rr=2.0,
            fvg=FVG(0, 0.95, 1.0, True, result.checked_at),
        )
        return result

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
        watcher.state.pairs = ["ETHUSD", "USDJPY"]
        watcher.journal = SignalJournal(db)
        watcher.notifier = self._FakeNotifier()
        watcher._send_chart = _no_chart
        watcher.news = None
        watcher.last_results = {}
        return watcher

    @pytest.mark.asyncio
    async def test_one_pairs_format_failure_does_not_stop_the_others(
        self, monkeypatch, tmp_path
    ):
        watcher = self._watcher(tmp_path)
        results = {"ETHUSD": self._approved("ETHUSD"), "USDJPY": self._approved("USDJPY")}

        async def fake_check_pair(key):
            return f"🚨 {key}: SETUP FOUND", results[key]

        monkeypatch.setattr(watcher, "check_pair", fake_check_pair)

        # This test is about alert-send isolation, not journal tracking —
        # the latter has its own test below. Without this stub,
        # journal.record() (called inside _send_alert before format_result
        # raises) leaves ETHUSD's signal "pending", and the real
        # _track_journal() at the end of run_cycle would then make a live
        # network fetch for it (tests must stay network-free).
        async def no_op_track_journal():
            return None

        monkeypatch.setattr(watcher, "_track_journal", no_op_track_journal)

        import smc_watcher as smc_watcher_mod

        real_format_result = smc_watcher_mod.format_result

        def raising_for_ethusd(result, in_plan=None):
            if result.symbol == "ETHUSD":
                raise ValueError("simulated formatting failure")
            return real_format_result(result, in_plan=in_plan)

        monkeypatch.setattr(smc_watcher_mod, "format_result", raising_for_ethusd)

        summary = await watcher.run_cycle()  # must not raise

        # ETHUSD's alert never reached the notifier ...
        assert not any("ETHUSD" in text for text in watcher.notifier.sent)
        # ... but USDJPY's still did, in the same cycle.
        assert any("USDJPY" in text for text in watcher.notifier.sent)
        # The cycle summary says so for both pairs, rather than silently
        # dropping the failing one.
        assert "ETHUSD: setup found but the alert failed to send" in summary
        assert "USDJPY: SETUP FOUND" in summary

    @pytest.mark.asyncio
    async def test_the_failing_pair_does_not_skip_journal_tracking(
        self, monkeypatch, tmp_path
    ):
        """Before the fix, an uncaught exception mid-loop would also skip
        `_track_journal()` at the end of `run_cycle` for every pair, not
        just the failing one — assert it still runs."""
        watcher = self._watcher(tmp_path)
        results = {"ETHUSD": self._approved("ETHUSD"), "USDJPY": self._approved("USDJPY")}

        async def fake_check_pair(key):
            return f"🚨 {key}: SETUP FOUND", results[key]

        monkeypatch.setattr(watcher, "check_pair", fake_check_pair)

        import smc_watcher as smc_watcher_mod

        def always_raises(result, in_plan=None):
            raise ValueError("simulated formatting failure")

        monkeypatch.setattr(smc_watcher_mod, "format_result", always_raises)

        track_calls = []

        async def spy_track_journal():
            track_calls.append(1)

        monkeypatch.setattr(watcher, "_track_journal", spy_track_journal)

        await watcher.run_cycle()  # must not raise even if every pair fails
        assert track_calls == [1]


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


class TestDataSourceFailureWarning:
    """The owner rotates his TwelveData key regularly. With the keyless
    fallback feed gone, an expired key means the forex fetch simply raises —
    and silence looks exactly like a quiet market. It must not (owner
    decision 2026-08-06: drop the keyless fallback, but a data outage must
    still speak)."""

    class _FakeNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, text, **kwargs):
            self.sent.append(text)
            return 1

    @staticmethod
    def _raising_fetcher(instrument):
        from app.core.exceptions import DataFetchError

        raise DataFetchError(f"TwelveData 401 for {instrument.key}")

    def _watcher(self, tmp_path):
        from app.services.smc.db import Database
        from app.services.smc.journal import SignalJournal
        from smc_watcher import Watcher

        db = Database(str(tmp_path / "smc.db"))
        watcher = Watcher.__new__(Watcher)
        watcher.db = db
        watcher.state = WatcherState(db)
        watcher.journal = SignalJournal(db)
        watcher.notifier = self._FakeNotifier()
        watcher.news = None
        watcher.last_results = {}
        return watcher

    @pytest.mark.asyncio
    async def test_data_source_failure_warns_once_per_hour(self, monkeypatch, tmp_path):
        watcher = self._watcher(tmp_path)
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.run_cycle()
        assert any("data source" in m.lower() for m in watcher.notifier.sent)
        watcher.notifier.sent.clear()
        await watcher.run_cycle()
        assert watcher.notifier.sent == [], "second failure inside the hour is quiet"

    @pytest.mark.asyncio
    async def test_throttle_survives_a_restart(self, monkeypatch, tmp_path):
        """source_warned is persisted like news_warned — a process restart
        must not re-warn immediately for a failure already reported."""
        watcher = self._watcher(tmp_path)
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.run_cycle()
        assert watcher.notifier.sent != []

        reloaded = self._watcher(tmp_path)  # fresh Watcher, same db file
        monkeypatch.setattr(reloaded, "_build_fetcher", self._raising_fetcher)
        await reloaded.run_cycle()
        assert reloaded.notifier.sent == [], "restart must not re-warn within the hour"

    @pytest.mark.asyncio
    async def test_a_different_pair_still_warns(self, monkeypatch, tmp_path):
        """Default watched pairs are ETHUSD + USDJPY — the throttle is keyed
        per pair, so both must warn on the same failing cycle."""
        watcher = self._watcher(tmp_path)
        assert watcher.state.pairs == ["ETHUSD", "USDJPY"]
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.run_cycle()
        assert any("ETHUSD" in m for m in watcher.notifier.sent)
        assert any("USDJPY" in m for m in watcher.notifier.sent)

    @pytest.mark.asyncio
    async def test_an_hour_old_stamp_does_not_latch_the_warning_off(
        self, monkeypatch, tmp_path
    ):
        """The throttle stores a timestamp, not a boolean 'already warned'
        flag — once an hour has passed since the last warning, the pair can
        warn again (proven here by back-dating the stored stamp, not by an
        actual recovery of the source)."""
        from datetime import datetime, timedelta, timezone

        watcher = self._watcher(tmp_path)
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.run_cycle()  # both pairs warn once
        assert len(watcher.notifier.sent) == 2
        watcher.notifier.sent.clear()

        # ETHUSD's warning is now over an hour old; USDJPY's stays fresh
        stale = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        watcher.state.source_warned["ETHUSD"] = stale.isoformat()
        watcher.state.save()

        await watcher.run_cycle()
        assert any("ETHUSD" in m for m in watcher.notifier.sent)
        assert not any("USDJPY" in m for m in watcher.notifier.sent)

    @pytest.mark.asyncio
    async def test_a_non_string_stamp_does_not_raise(self, tmp_path):
        """The throttle reads the stored stamp with
        `datetime.fromisoformat(last)` inside a `try/except (ValueError,
        TypeError)` guard. A corrupt or hand-edited kv store could hold
        something that is not a string at all (e.g. a stray int from a bad
        migration) — fromisoformat raises TypeError for that, not
        ValueError, so the guard must catch both. The warning must still
        fire rather than the throttle silently eating the failure."""
        watcher = self._watcher(tmp_path)
        watcher.state.source_warned["ETHUSD"] = 12345  # not a string
        await watcher._warn_data_source_failure("ETHUSD", "simulated failure")
        assert any("ETHUSD" in m for m in watcher.notifier.sent)

    @pytest.mark.asyncio
    async def test_a_send_failure_does_not_start_the_quiet_window(
        self, monkeypatch, tmp_path
    ):
        """TelegramNotifier.send returns None on any API/network failure
        without raising. If the throttle stamped itself anyway, a Telegram
        hiccup at the moment of the first warning would buy the owner an
        hour of silence he never actually saw."""

        class _FailingNotifier:
            def __init__(self):
                self.sent = []

            async def send(self, text, **kwargs):
                self.sent.append(text)
                return None  # Telegram/API failure

        watcher = self._watcher(tmp_path)
        watcher.notifier = _FailingNotifier()
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.run_cycle()
        assert watcher.notifier.sent  # the send was attempted
        assert watcher.state.source_warned == {}, (
            "a warning that never reached Telegram must not throttle the next one"
        )

    @pytest.mark.asyncio
    async def test_eth_hint_does_not_mention_an_api_key(self, monkeypatch, tmp_path):
        """Binance (ETHUSD) is keyless — telling the owner to check an API
        key that does not exist, for what is really a transient network
        error, is actively misleading."""
        watcher = self._watcher(tmp_path)
        watcher.state.pairs = ["ETHUSD"]

        def _raise_for_eth(instrument):
            from app.core.exceptions import DataFetchError

            raise DataFetchError("Binance klines request failed")

        monkeypatch.setattr(watcher, "_build_fetcher", _raise_for_eth)
        await watcher.run_cycle()
        assert watcher.notifier.sent
        assert "API key" not in watcher.notifier.sent[0]

    @pytest.mark.asyncio
    async def test_forex_hint_still_mentions_the_api_key(self, monkeypatch, tmp_path):
        watcher = self._watcher(tmp_path)
        watcher.state.pairs = ["USDJPY"]
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.run_cycle()
        assert watcher.notifier.sent
        assert "API key" in watcher.notifier.sent[0]

    @pytest.mark.asyncio
    async def test_plan_warns_on_a_dead_source_even_while_paused(
        self, monkeypatch, tmp_path
    ):
        """/plan has no pause gate, and run_cycle's own warning never fires
        while paused (it returns before any fetch) — a dead source must
        still reach the owner through the command he starts his day with.
        (A news blackout removes the cycle's cover the same way — blackout
        skips a pair before check_pair runs, but /plan has no blackout gate
        either — so this one code path covers both circumstances.)"""
        watcher = self._watcher(tmp_path)
        watcher.state.paused = True
        monkeypatch.setattr(watcher, "_build_fetcher", self._raising_fetcher)
        await watcher.on_plan("ETHUSD")
        assert any("data source" in m.lower() for m in watcher.notifier.sent)

    @pytest.mark.asyncio
    async def test_heartbeat_line_escapes_the_exception_detail(
        self, monkeypatch, tmp_path
    ):
        """check_pair's returned heartbeat line is fed into a parse_mode=HTML
        message whenever SMC_NOTIFY_NO_SETUP=true or /check is used. A raw
        '<' in a fetcher error message once broke Telegram delivery in
        production (CLAUDE.md) — this failure path is now hit routinely (any
        cycle with an expired key), so it must go through escape_html too."""

        def _raise_with_angle_bracket(instrument):
            from app.core.exceptions import DataFetchError

            raise DataFetchError("fill < 50% for " + instrument.key)

        watcher = self._watcher(tmp_path)
        monkeypatch.setattr(watcher, "_build_fetcher", _raise_with_angle_bracket)
        line, result = await watcher.check_pair("ETHUSD")
        assert result is None
        assert "<" not in line
        assert "&lt;" in line
