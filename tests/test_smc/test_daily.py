"""D29 (owner decision 2026-09-13): the daily candle, label-only. The daily
trend is printed and marked, the daily pools join the ladder and the
charts draw PDH/PDL and PWH/PWL — Rule 1 and Rule 7 read what they read
before, and every fetcher serves D1 best-effort."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import DataFetchError
from app.services.smc import i18n
from app.services.smc.ai_read import describe_for_ai
from app.services.smc.chart import render_plan_chart, render_setup_chart
from app.services.smc.data import BinanceDataFetcher
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.notifier import against_d1, format_result, format_setup_analysis
from app.services.smc.pending import build_pending
from app.services.smc.plan import build_plan
from app.services.smc.sniper import daily_levels
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)

ETH = get_instrument("ETHUSD")
T0 = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)
D1_UP = make_candles([c * 1.1 for c in H4_UPTREND_CLOSES], step_minutes=1440)
D1_DOWN = make_candles([3400 - (c - 3000) for c in H4_UPTREND_CLOSES], step_minutes=1440)
# a daily series whose last day and last week sit INSIDE the M5/H1 chart
# windows (3128-3160 / 3100-3221), so the dotted levels actually land on the
# picture — D1_UP's levels sit hundreds of dollars above and stay off-chart
D1_NEAR = make_candles(
    [3100, 3140, 3120, 3155, 3135, 3150, 3130, 3145, 3138, 3152, 3134, 3148],
    step_minutes=1440,
)


def _run(d1=None, m5=None):
    m5 = m5 or m5_long_trigger()
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=T0)
    r.session_name = "Frankfurt/London"
    r.price = m5[-1].close
    r = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(h4=h4, h1=h1, m5=m5, result=r, d1=d1)
    r.h4_candles, r.h1_candles, r.m5_candles, r.d1_candles = h4, h1, m5, d1
    return r, h4, h1, m5


class TestEngine:
    def test_daily_trend_is_a_label_and_the_direction_is_unchanged(self):
        with_d1, *_ = _run(D1_DOWN)
        without, *_ = _run(None)
        assert with_d1.d1_trend == Trend.DOWN and without.d1_trend is None
        assert with_d1.setup.direction == Direction.LONG == without.setup.direction
        assert with_d1.verdict == without.verdict
        assert against_d1(with_d1.d1_trend, with_d1.setup.direction) is True

    def test_daily_pools_join_the_ladder_but_not_rule_7(self):
        with_d1, *_ = _run(D1_UP)
        without, *_ = _run(None)
        assert any(lv.timeframe == "D1" for lv in with_d1.setup.ladder)
        assert not any(lv.timeframe == "D1" for lv in without.setup.ladder)
        assert with_d1.setup.take_profit == without.setup.take_profit
        assert with_d1.setup.target.price == without.setup.target.price
        assert with_d1.setup.tier_star == without.setup.tier_star

    def test_pending_targets_see_the_daily_pools(self):
        result, h4, h1, m5 = _run(D1_UP)
        audit = build_pending(result, ETH, h4, h1, m5, d1=D1_UP)
        prices = {tp.price for e in audit.entries for tp in e.targets}
        plain = build_pending(result, ETH, h4, h1, m5)
        assert prices >= {tp.price for e in plain.entries for tp in e.targets}


class TestLevels:
    def test_pdh_pdl_and_previous_week(self):
        levels = daily_levels(D1_UP, T0)
        assert levels["pdh"] == D1_UP[-1].high and levels["pdl"] == D1_UP[-1].low
        assert levels["pwh"] is not None and levels["pwl"] is not None
        assert levels["pwh"] > levels["pwl"]

    def test_slices_at_as_of_and_handles_nothing(self):
        assert daily_levels([], T0) is None
        early = daily_levels(D1_UP, D1_UP[3].timestamp + timedelta(days=1))
        assert early["pdh"] == D1_UP[3].high  # only days that had closed
        assert daily_levels(D1_UP, D1_UP[0].timestamp) is None


class TestMessages:
    def test_card_and_audit_name_the_daily_trend_and_the_conflict(self):
        result, h4, h1, m5 = _run(D1_DOWN)
        card = format_result(result)
        assert "H4 up · H1 flat · D1 down ⚠️ against D1" in card
        audit = format_setup_analysis(
            "ETHUSD", result, build_pending(result, ETH, h4, h1, m5, d1=D1_DOWN), ETH,
        )
        assert "· D1 downtrend ⚠️ against D1" in audit

    def test_agreeing_or_absent_daily_trend_carries_no_marker(self):
        agree, *_ = _run(D1_UP)
        assert "· D1 up" in format_result(agree) and "against D1" not in format_result(agree)
        none, *_ = _run(None)
        assert "D1" not in format_result(none).split("\n")[1]

    def test_russian_marker(self):
        i18n.set_language("ru")
        try:
            result, *_ = _run(D1_DOWN)
            assert "⚠️ против D1" in format_result(result)
        finally:
            i18n.set_language("en")

    def test_fact_sheet_carries_the_daily_context(self):
        result, *_ = _run(D1_UP)
        text = describe_for_ai(result, ETH, candles=True)
        assert "D1 trend: up" in text
        assert "Daily levels (D1): PDH" in text and "previous week high" in text
        assert "Recent D1 candles" in text
        none, *_ = _run(None)
        text = describe_for_ai(none, ETH)
        assert "D1 trend: n/a" in text and "Recent D1 candles" not in text


class TestCharts:
    def test_daily_levels_are_drawn_on_both_charts(self):
        result, h4, h1, m5 = _run(D1_NEAR)
        with_d1 = render_setup_chart(result)
        result.d1_candles = None
        assert with_d1 != render_setup_chart(result) and with_d1[:4] == b"\x89PNG"
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        # the plan chart slices D1 at the last H1 candle, so the daily
        # series must have closed days BEFORE the H1 fixture (as in
        # production, where D1 history precedes the H1 window)
        from dataclasses import replace

        past = [replace(c, timestamp=c.timestamp - timedelta(days=14)) for c in D1_NEAR]
        assert render_plan_chart(plan, h1, d1=past) != render_plan_chart(plan, h1)

    def test_off_window_daily_levels_do_not_flatten_the_chart(self):
        # D1_UP's PDH/PDL sit ~450 above the M5 window: they are skipped,
        # not drawn at the edge, and the y-axis stays on the candles
        result, *_ = _run(D1_UP)
        with_d1 = render_setup_chart(result)
        result.d1_candles = None
        assert with_d1 == render_setup_chart(result)


class TestFetchers:
    def test_binance_d1_is_best_effort(self):
        fetcher = BinanceDataFetcher("ETHUSDT")

        async def fake(interval, limit=300):
            if interval == "1d":
                raise DataFetchError("boom")
            return make_candles([1.0, 2.0], step_minutes=5)

        fetcher.fetch_candles = fake
        data = asyncio.run(fetcher.fetch_all_timeframes())
        assert data["d1"] == [] and len(data["h4"]) == 2

    def test_binance_serves_d1_when_it_can(self):
        fetcher = BinanceDataFetcher("ETHUSDT")

        async def fake(interval, limit=300):
            assert interval != "1d" or limit == 120
            return make_candles([1.0, 2.0, 3.0], step_minutes=5)

        fetcher.fetch_candles = fake
        assert len(asyncio.run(fetcher.fetch_all_timeframes())["d1"]) == 3

    def test_twelvedata_and_oanda_know_the_daily_interval(self):
        from app.services.smc import history, oanda, twelvedata

        assert twelvedata._INTERVAL["1d"] == "1day"
        assert twelvedata._TF_CACHE_TTL["1d"] >= timedelta(hours=1)
        assert oanda.GRANULARITY["1d"] == "D"
        assert history._FETCH_INTERVAL["d1"] == "1d" and history.CANDLE_MINUTES["d1"] == 1440


class TestBacktest:
    def test_replay_never_shows_an_unclosed_day(self):
        from app.services.smc.backtest import run_backtest, synthetic_history
        from app.services.smc.db import Database
        from app.services.smc.journal import SignalJournal
        from app.services.smc.profiles import CONSERVATIVE
        import tempfile
        import os

        class Recorder:
            profile = CONSERVATIVE

            def __init__(self):
                self.seen = []

            def evaluate(self, h4, h1, m5, result, d1=None, **kw):
                self.seen.append((result.checked_at, d1))
                return result

        hist = synthetic_history(days=6)
        h4, h1, m5 = hist["h4"], hist["h1"], hist["m5"]
        d1 = make_candles([c.close for c in h4[::6]], step_minutes=1440)
        # align the daily series with the synthetic window
        d1 = [type(c)(timestamp=h4[0].timestamp + timedelta(days=i), open=c.open, high=c.high, low=c.low, close=c.close)
              for i, c in enumerate(d1)]
        engine = Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            j = SignalJournal(Database(os.path.join(tmp, "b.db")))
            run_backtest("ETHUSD", h4, h1, m5, m5[0].timestamp, m5[-1].timestamp, journal=j,
                         engine=engine, require_full_windows=False, d1=d1)
        assert engine.seen
        for now, view in engine.seen:
            if view:
                assert view[-1].timestamp + timedelta(days=1) <= now
