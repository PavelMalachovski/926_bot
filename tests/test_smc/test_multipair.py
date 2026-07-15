"""Tests for instruments registry, OANDA parsing, state and correlation guard."""

import json

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

        monkeypatch.setattr(settings.oanda, "api_token", None)
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
