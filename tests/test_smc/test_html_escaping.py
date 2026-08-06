"""Regression tests: Telegram HTML must not be broken by dynamic text.

Production incident 2026-07-16: a /check summary containing the engine reason
"fill < 50%" was rejected by Telegram with `can't parse entities: Unsupported
start tag` because `<` was sent unescaped in parse_mode=HTML.
"""

import re
from datetime import datetime, timezone

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.liquidity import LiquidityLevel
from app.services.smc.models import (
    AnalysisResult, Direction, FVG, TradeSetup, Trend, Verdict, Zone,
)
from app.services.smc.news import NewsCalendar, parse_feed
from app.services.smc.notifier import (
    escape_html,
    format_distance,
    format_no_setup,
    format_result,
    format_target,
)
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger_deep_sweep,
    make_candles,
)


def _assert_valid_telegram_html(text: str):
    """Every '<' must start one of the tags we intentionally use."""
    for match in re.finditer(r"<", text):
        following = text[match.start():match.start() + 4]
        assert re.match(r"</?b>", following), (
            f"unescaped '<' near: ...{text[max(0, match.start() - 20):match.start() + 10]}..."
        )
    assert "&" not in re.sub(r"&(amp|lt|gt);", "", text), "unescaped '&'"


def _result_with_reason(reason: str, verdict=Verdict.WATCH) -> AnalysisResult:
    result = AnalysisResult(
        symbol="USDJPY",
        verdict=verdict,
        checked_at=datetime(2026, 7, 16, 7, 12, tzinfo=timezone.utc),
    )
    result.reasons = [reason]
    result.watch_notes = [reason]
    return result


class TestEscaping:
    def test_escape_html_basics(self):
        assert escape_html("fill < 50%") == "fill &lt; 50%"
        assert escape_html("S&P Global PMI") == "S&amp;P Global PMI"
        assert escape_html("a > b") == "a &gt; b"

    def test_production_incident_fill_lt_50(self):
        reason = "M5 CHoCH is there, but no valid FVG (size ≥ 5 pips, fill < 50%, current session)"
        line = format_no_setup(_result_with_reason(reason))
        _assert_valid_telegram_html(line)
        assert "fill &lt; 50%" in line

    def test_format_result_watch_and_skip_escaped(self):
        reason = "RR 1:1.4 < minimum 1:2 to the nearest H1/H4 zones"
        for verdict in (Verdict.WATCH, Verdict.SKIP):
            text = format_result(_result_with_reason(reason, verdict))
            _assert_valid_telegram_html(text)

    def test_aggressive_profile_tag_shown_only_for_aggressive(self):
        result = _result_with_reason("no direction")
        result.profile_key = "aggressive"
        text = format_result(result)
        _assert_valid_telegram_html(text)
        assert "⚡ <b>Aggressive profile</b>" in text

        result.profile_key = "conservative"
        text = format_result(result)
        assert "Aggressive profile" not in text

    def test_news_digest_titles_escaped(self):
        cal = NewsCalendar()
        cal.events = parse_feed(
            [
                {
                    "title": "S&P Global Manufacturing PMI",
                    "country": "USD",
                    "date": "2026-07-16T09:45:00-04:00",
                    "impact": "High",
                }
            ]
        )
        cal.fetched_at = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)
        text = cal.digest_text(
            ["ETHUSD"], datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
        )
        _assert_valid_telegram_html(text)
        assert "S&amp;P" in text


class TestTargetLine:
    def test_rr_line_names_the_liquidity(self):
        level = LiquidityLevel(
            price=3221.0, is_high=True, timeframe="H1",
            equal_count=1, timestamp=None,
        )
        assert format_target(level, 2) == "H1 swing high 3221.00"

    def test_pool_size_is_shown(self):
        level = LiquidityLevel(
            price=3221.0, is_high=True, timeframe="H1",
            equal_count=3, timestamp=None,
        )
        assert format_target(level, 2) == "H1 swing high 3221.00 (EQH x3)"

    def test_low_pool_uses_eql(self):
        level = LiquidityLevel(
            price=3050.0, is_high=False, timeframe="H4",
            equal_count=2, timestamp=None,
        )
        assert format_target(level, 2) == "H4 swing low 3050.00 (EQL x2)"


def _hand_built(**setup_kwargs) -> AnalysisResult:
    """A hand-built approved result, for paths the synthetic candles do not
    reach (a ladder with no order block, an empty ladder, and so on)."""
    result = AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.APPROVED_LIMIT,
        checked_at=datetime(2026, 7, 16, 7, 12, tzinfo=timezone.utc),
        price=3200.0,
        h4_trend=Trend.UP,
        price_decimals=2,
    )
    result.session_name = "New York"
    fvg = FVG(
        index=10, bottom=3100.0, top=3150.0, is_bullish=True,
        timestamp=datetime(2026, 7, 16, 7, 5, tzinfo=timezone.utc),
    )
    kwargs = dict(
        direction=Direction.LONG,
        entry=3125.0,
        stop_loss=3050.0,
        take_profit=3300.0,
        rr=2.0,
        fvg=fvg,
        target=None,
    )
    kwargs.update(setup_kwargs)
    result.setup = TradeSetup(**kwargs)
    return result


def _approved(min_rr=1.0, max_entry_gap_r=99.0, price=None, no_liquidity=False,
              monkeypatch=None):
    """A real APPROVED result, produced by the engine rather than hand-built —
    the alert must render what the engine actually emits."""
    if no_liquidity:
        import app.services.smc.engine as E
        monkeypatch.setattr(E, "nearest_liquidity", lambda *a, **k: None)
        monkeypatch.setattr(E, "liquidity_ladder", lambda *a, **k: [])
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )
    result.session_name = "New York"
    if price is not None:
        result.price = price
    engine = TripleSyncEngine(min_fvg_size=2.0, sl_buffer=2.0,
                              min_rr=min_rr, max_entry_gap_r=max_entry_gap_r)
    return engine.evaluate(
        h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5=m5_long_trigger_deep_sweep(), result=result,
    )


class TestDetectorAlert:

    def test_four_actionable_lines_come_first(self):
        text = format_result(_approved())
        head = text.split("🎯")[0]
        for marker in ("H1 Demand zone", "M5 imbalance", "M5 order block",
                       "Swept liquidity"):
            assert marker in head

    def test_ladder_shows_both_rr_columns(self):
        text = format_result(_approved())
        assert "RR from FVG / from OB" in text
        assert text.count("1:") >= 5

    def test_warnings_render_between_levels_and_reference(self):
        text = format_result(_approved(min_rr=8.0))
        assert text.index("⚠️") > text.index("🎯")
        assert text.index("⚠️") < text.index("ref ·")

    def test_no_take_profit_renders_without_a_bogus_level(self, monkeypatch):
        text = format_result(_approved(no_liquidity=True, monkeypatch=monkeypatch))
        assert "no unswept liquidity ahead" in text
        assert "🎯" not in text or "—" in text

    def test_output_is_valid_telegram_html(self):
        _assert_valid_telegram_html(format_result(_approved(min_rr=8.0)))

    def test_every_warning_at_once_is_still_valid_html(self):
        """Spec §9: one rendered alert carrying all the warnings at once."""
        result = _approved(min_rr=8.0)
        result.warnings = [
            "price has run 1.3R past the imbalance",
            "RR to the nearest liquidity is 1:0.4",
            "no unswept liquidity ahead",
            "nearest liquidity sits inside the stop buffer",
            "data source degraded: 3 < 5 candles & counting",
        ]
        result.setup.entry_is_market = True
        text = format_result(result)
        _assert_valid_telegram_html(text)
        for warning in result.warnings[:4]:
            assert escape_html(warning) in text
        assert "3 &lt; 5 candles &amp; counting" in text
        assert "▶️ price is inside the imbalance right now" in text

    def test_distance_units_follow_the_instrument(self):
        assert format_distance(10.08, get_instrument("ETHUSD")) == "$10.08"
        # USDJPY pip = 0.01, so 0.038 price units = 3.8 pips.
        assert format_distance(0.038, get_instrument("USDJPY")) == "3.8 pips"
        # 0.00038 is 0.038 pips — a real sub-tenth-pip distance rounds to 0.0.
        assert format_distance(0.00038, get_instrument("USDJPY")) == "0.0 pips"

    def test_header_names_the_direction_source(self):
        result = _approved()
        assert "H4 uptrend" in format_result(result)

        result.direction_source = "h1"
        assert "H4 flat — direction from H1" in format_result(result)

        result.direction_source = "h4_choch"
        text = format_result(result)
        assert "direction from CHoCH (first leg, not with-trend)" in text

    def test_plan_provenance_is_omitted_unless_the_caller_knows(self):
        result = _approved()
        assert "plan" not in format_result(result)
        assert "from this morning's plan" in format_result(result, in_plan=True)
        assert "not in the plan" in format_result(result, in_plan=False)

    def test_single_rr_column_without_an_order_block(self):
        result = _hand_built(
            order_block=None,
            ladder=[
                LiquidityLevel(price=3221.0, is_high=True, timeframe="H1",
                               equal_count=3, timestamp=None),
            ],
        )
        text = format_result(result)
        _assert_valid_telegram_html(text)
        assert "RR from FVG" in text
        assert "from OB" not in text
        assert "M5 order block" not in text

    def test_empty_ladder_says_nothing_is_ahead(self):
        result = _hand_built(take_profit=None, rr=0.0, ladder=[])
        result.warnings.append("no unswept liquidity ahead")
        text = format_result(result)
        _assert_valid_telegram_html(text)
        assert "none ahead" in text

    def test_zones_ahead_block_only_when_present(self):
        result = _hand_built(ladder=[])
        assert "Untested zones further out" not in format_result(result)

        result.setup.zones_ahead = [
            Zone(bottom=3300.0, top=3320.0, is_demand=False, pivot_index=4,
                 timestamp=datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)),
        ]
        text = format_result(result)
        _assert_valid_telegram_html(text)
        assert "Untested zones further out" in text
        assert "3300.00 – 3320.00" in text
        assert "0 touches" in text
