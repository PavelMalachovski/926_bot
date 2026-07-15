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
    def test_defaults_when_no_file(self, tmp_path):
        state = WatcherState(str(tmp_path / "state.json"))
        assert state.pairs == ["ETHUSD", "USDJPY"]

    def test_toggle_and_persist(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = WatcherState(path)
        assert state.toggle_pair("EURUSD") is True
        assert state.toggle_pair("USDJPY") is False
        assert state.pairs == ["ETHUSD", "EURUSD"]

        reloaded = WatcherState(path)
        assert reloaded.pairs == ["ETHUSD", "EURUSD"]

    def test_unknown_pairs_in_file_are_dropped(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"pairs": ["ETHUSD", "DOGEUSD"]}))
        state = WatcherState(str(path))
        assert state.pairs == ["ETHUSD"]


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
        assert any("EURUSD и GBPUSD" in w for w in warnings)

    def test_triple_usd_bet_forbidden(self):
        from smc_watcher import _correlation_warnings

        warnings = _correlation_warnings(
            [self._approved("GBPUSD", "long"), self._approved("USDJPY", "short")]
        )
        assert any("тройная" in w for w in warnings)

    def test_allowed_combination_is_silent(self):
        from smc_watcher import _correlation_warnings

        warnings = _correlation_warnings(
            [self._approved("ETHUSD", "long"), self._approved("USDJPY", "long")]
        )
        assert warnings == []
