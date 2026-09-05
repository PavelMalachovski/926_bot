"""Pending (limit) entries and the approach read — owner decision D25
(2026-09-05). Pure geometry over an engine result: `take_profits` (TP1-3 off
the liquidity ladder), `build_pending` (MAIN/DEEP rungs for the Setup-analysis
button) and `approach_read` (the "price is almost there" moment)."""

from datetime import datetime, timezone

import pytest

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.liquidity import LiquidityLevel, take_profits
from app.services.smc.models import (
    FVG,
    AnalysisResult,
    Direction,
    TradeSetup,
    Trend,
    Verdict,
    Zone,
)
from app.services.smc.pending import (
    ROLE_BOUNDARY,
    ROLE_MARKET,
    approach_read,
    build_pending,
)
from app.services.smc.range import Range
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)

ETH = get_instrument("ETHUSD")
T0 = datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc)


def _fresh(price=0.0):
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=T0)
    r.session_name = "New York"
    r.price = price
    return r


def _h4():
    return make_candles(H4_UPTREND_CLOSES, step_minutes=240)


def _h1():
    return make_candles(H1_PULLBACK_CLOSES, step_minutes=60)


def _evaluated(m5):
    h4, h1 = _h4(), _h1()
    result = _fresh(price=m5[-1].close)
    result = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(
        h4=h4, h1=h1, m5=m5, result=result
    )
    result.h4_candles, result.h1_candles, result.m5_candles = h4, h1, m5
    return result, h4, h1, m5


def _level(price, is_high=True, tf="H1", n=1):
    return LiquidityLevel(price=price, is_high=is_high, timeframe=tf, equal_count=n)


# ------------------------------------------------------------ take_profits


class TestTakeProfits:
    LEVELS = [
        _level(3305.0, n=2), _level(3400.0, tf="H4"), _level(3450.0),
        _level(3500.0), _level(3000.0, is_high=False),
    ]

    def test_three_nearest_pools_one_buffer_short_with_rr_from_the_entry(self):
        tps = take_profits(self.LEVELS, Direction.LONG, 3200.0, 3100.0, 2.0)
        assert [tp.price for tp in tps] == [3303.0, 3398.0, 3448.0]
        assert [round(tp.rr, 2) for tp in tps] == [1.03, 1.98, 2.48]
        assert tps[0].level.equal_count == 2  # the EQH pool is named

    def test_a_pool_inside_the_buffer_is_skipped_not_printed(self):
        levels = [_level(3201.0), _level(3300.0)]
        tps = take_profits(levels, Direction.LONG, 3200.0, 3100.0, 2.0)
        assert [tp.price for tp in tps] == [3298.0]

    def test_short_side_mirrors(self):
        levels = [_level(3100.0, is_high=False), _level(3000.0, is_high=False)]
        tps = take_profits(levels, Direction.SHORT, 3200.0, 3250.0, 2.0)
        assert [tp.price for tp in tps] == [3102.0, 3002.0]
        assert round(tps[0].rr, 2) == 1.96

    def test_zero_risk_yields_nothing(self):
        assert take_profits(self.LEVELS, Direction.LONG, 3200.0, 3200.0, 2.0) == []


# ----------------------------------------------------------- build_pending


class TestBuildPendingOnACompletedSetup:
    def test_main_is_the_rule_5_entry_and_deep_goes_further_out(self):
        result, h4, h1, m5 = _evaluated(m5_long_trigger())
        assert result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)
        analysis = build_pending(result, ETH, h4, h1, m5)

        assert analysis.direction == Direction.LONG
        main, deep = analysis.main, analysis.deep
        assert main is not None and deep is not None
        assert main.entry == pytest.approx(result.setup.entry)  # Rule 5 rung
        assert main.stop_loss == pytest.approx(result.setup.stop_loss)  # Rule 6
        assert deep.entry < main.entry  # deeper for a LONG
        # a deeper rung never carries a tighter stop than the setup's own
        assert deep.stop_loss <= result.setup.stop_loss
        for e in analysis.entries:
            assert e.entry < result.price  # a limit rests below price
            assert e.targets, "TP1-3 priced from the M5/H1/H4 ladder"
            assert [tp.price for tp in e.targets] == sorted(
                tp.price for tp in e.targets
            )

    def test_market_reference_is_price_now_with_the_setup_stop(self):
        result, h4, h1, m5 = _evaluated(m5_long_trigger())
        analysis = build_pending(result, ETH, h4, h1, m5)
        market = analysis.market
        assert market is not None and market.role == ROLE_MARKET
        assert market.entry == pytest.approx(result.price)
        assert market.stop_loss == pytest.approx(result.setup.stop_loss)
        assert market.targets and market.targets[0].price > result.price

    def test_price_that_ran_past_every_rung_leaves_only_the_market(self):
        r = _fresh(price=3100.0)  # LONG, price BELOW the imbalance entry
        r.verdict = Verdict.APPROVED_MARKET
        r.h4_trend = r.h1_trend = Trend.UP
        r.setup = TradeSetup(
            direction=Direction.LONG, entry=3125.0, stop_loss=3050.0,
            take_profit=None, rr=0.0,
            fvg=FVG(10, 3100.0, 3150.0, True, T0),
        )
        analysis = build_pending(r, ETH, _h4(), _h1(), make_candles([3100.0]))
        assert analysis.entries == []
        assert "market entry" in analysis.note
        assert analysis.market is not None


class TestBuildPendingWhileWaiting:
    def test_pullback_phase_prices_the_h1_zone_bracket(self):
        result, h4, h1, m5 = _evaluated(make_candles([3160.0]))
        assert result.verdict == Verdict.WATCH and result.h1_zone is not None
        analysis = build_pending(result, ETH, h4, h1, m5)

        main = analysis.main
        assert main is not None and main.direction == Direction.LONG
        assert main.label == "H1 Demand OB"
        # plan geometry: the near edge, one buffer beyond the far edge
        assert main.entry == pytest.approx(result.h1_zone.top)
        assert main.stop_loss == pytest.approx(result.h1_zone.bottom - ETH.sl_buffer)
        assert main.zone == (result.h1_zone.bottom, result.h1_zone.top)
        assert main.targets and all(tp.price > main.entry for tp in main.targets)
        assert analysis.market is None  # nothing has formed yet

    def test_no_direction_means_no_entries_and_a_reason(self):
        r = _fresh(price=3160.0)
        r.verdict = Verdict.SKIP
        r.reasons = ["H4 is flat or CHoCH against the trend — no direction"]
        analysis = build_pending(r, ETH, _h4(), _h1(), make_candles([3160.0]))
        assert analysis.entries == [] and analysis.direction is None
        assert analysis.note == r.reasons[0]


def _box(top=3200.0, bottom=3100.0):
    return Range(
        top=top, bottom=bottom, touches_top=2, touches_bottom=2, broken=False,
        top_at=T0, bottom_at=T0, swept_top=False, swept_bottom=False,
        printed_at=T0,
    )


class TestBuildPendingMidRange:
    def test_one_bracket_per_boundary_aiming_at_the_other(self):
        r = _fresh(price=3150.0)
        r.verdict = Verdict.WATCH
        r.market_range = _box()
        r.reasons = ["mid-range"]
        analysis = build_pending(r, ETH, _h4(), _h1(), make_candles([3150.0]))

        assert analysis.range_mode and len(analysis.entries) == 2
        short, long_ = analysis.entries
        assert short.role == long_.role == ROLE_BOUNDARY
        assert short.direction == Direction.SHORT and short.entry == 3200.0
        assert short.stop_loss == 3202.0
        assert [tp.price for tp in short.targets] == [3102.0]  # D14: one target
        assert long_.direction == Direction.LONG and long_.entry == 3100.0
        assert long_.stop_loss == 3098.0
        assert [tp.price for tp in long_.targets] == [3198.0]


# ----------------------------------------------------------- approach_read


class TestApproachRead:
    """Zone 3131-3138 (height 7): 'almost there' is within one zone-height
    of the near edge, floored at 2 x min FVG ($4)."""

    def _watch(self, price):
        result, h4, h1, _ = _evaluated(make_candles([price]))
        return result, h4, h1

    def test_within_one_zone_height_reads_as_an_approach(self):
        result, h4, h1 = self._watch(3144.0)
        a = approach_read(result, ETH, h4, h1, factor=1.0)
        assert a is not None and not a.inside
        assert a.direction == Direction.LONG and a.kind == "OB"
        assert a.distance == pytest.approx(3144.0 - 3138.0)
        assert a.bracket.entry == 3138.0 and a.bracket.stop_loss == 3129.0
        assert a.bracket.targets

    def test_further_than_the_factor_allows_is_not_an_approach(self):
        result, h4, h1 = self._watch(3146.0)
        assert approach_read(result, ETH, h4, h1, factor=1.0) is None
        result, h4, h1 = self._watch(3144.0)
        assert approach_read(result, ETH, h4, h1, factor=0.5) is None

    def test_inside_the_zone_counts_as_distance_zero(self):
        result, h4, h1 = self._watch(3135.0)
        a = approach_read(result, ETH, h4, h1)
        assert a is not None and a.inside and a.distance == 0.0

    def test_price_beyond_the_zone_is_not_an_approach(self):
        result, h4, h1 = self._watch(3144.0)
        result.price = 3120.0  # below a demand zone: price left it behind
        assert approach_read(result, ETH, h4, h1) is None

    def test_only_a_watch_qualifies(self):
        result, h4, h1, _ = _evaluated(m5_long_trigger())
        assert result.verdict != Verdict.WATCH
        assert approach_read(result, ETH, h4, h1) is None

    def test_mid_range_reads_the_nearer_boundary(self):
        r = _fresh(price=3195.0)
        r.verdict = Verdict.WATCH
        r.market_range = _box()
        a = approach_read(r, ETH, _h4(), _h1(), factor=1.0)
        assert a is not None and a.kind == "RANGE"
        assert a.direction == Direction.SHORT
        assert a.bracket.entry == 3200.0 and a.bracket.stop_loss == 3202.0
        assert [tp.price for tp in a.bracket.targets] == [3102.0]
        r.price = 3150.0  # dead centre: nowhere near either edge
        assert approach_read(r, ETH, _h4(), _h1(), factor=1.0) is None


# ------------------------------------------------------------- the messages


class TestD25Messages:
    """Both new messages obey the one Telegram rule that once broke delivery
    in production: only <b>/<pre> tags, every dynamic value escaped."""

    @staticmethod
    def _valid_html(text):
        import re

        for m in re.finditer(r"<", text):
            assert re.match(r"</?(b|pre)>", text[m.start():m.start() + 6]), text
        assert "&" not in re.sub(r"&(amp|lt|gt);", "", text)

    def test_setup_analysis_table_is_valid_html_and_carries_both_columns(self):
        from app.services.smc.notifier import format_setup_analysis

        result, h4, h1, m5 = _evaluated(m5_long_trigger())
        analysis = build_pending(result, ETH, h4, h1, m5)
        analysis.entries[0].label = "M5 FVG <edge> & co"  # a hostile label
        text = format_setup_analysis("ETHUSD", result, analysis, ETH, as_of="15:40")
        self._valid_html(text)
        assert "🔬 <b>Setup analysis — ETHUSD</b> · LONG · H4 up · H1 flat" in text
        assert "🚨 <b>Setup formed</b> — market entry" in text
        assert "MAIN" in text and "DEEP" in text
        assert "M5 FVG &lt;edge&gt; &amp; co" in text
        assert "TP1" in text and "Rule 10" in text

    def test_setup_analysis_with_nothing_pending_names_the_reason(self):
        from app.services.smc.notifier import format_setup_analysis

        r = _fresh(price=3160.0)
        r.verdict = Verdict.SKIP
        r.reasons = ["H4 is flat <or> CHoCH against the trend — no direction"]
        analysis = build_pending(r, ETH, _h4(), _h1(), make_candles([3160.0]))
        text = format_setup_analysis("ETHUSD", r, analysis, ETH)
        self._valid_html(text)
        assert "No pending entry to place" in text and "&lt;or&gt;" in text

    def test_approach_message_is_valid_html(self):
        from app.services.smc.notifier import format_approach_alert

        result, h4, h1, _ = _evaluated(make_candles([3144.0]))
        a = approach_read(result, ETH, h4, h1)
        a.kind = "OB<x>"
        text = format_approach_alert("ETH<USD", a, ETH)
        self._valid_html(text)
        assert "ETH&lt;USD" in text and "OB&lt;x&gt;" in text
