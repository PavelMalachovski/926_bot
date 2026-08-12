"""Phase 2 sniper redesign, Task 4: two-tier alerting — the ⭐ star card and
the quiet non-star one-liner.

Star: `_format_detector_alert` gains a `⭐ SNIPER` header plus TP1/runner
lines when `setup.tier_star`; the existing ladder/warnings/HTML-escaping
discipline stays untouched. Non-star: `format_quiet_setup` renders a short
plain message instead — no `<pre>` ladder, listing `tier_missed`.
"""

import re
from datetime import datetime, timezone

from app.services.smc.models import (
    AnalysisResult, Direction, FVG, TradeSetup, Trend, Verdict,
)
from app.services.smc.notifier import (
    escape_html,
    format_quiet_setup,
    format_result,
)


def _assert_valid_telegram_html(text: str):
    """Same narrow contract as test_html_escaping.py: only <b>/<pre> tags,
    everything else must have gone through escape_html."""
    for match in re.finditer(r"<", text):
        following = text[match.start():match.start() + 6]
        assert re.match(r"</?(b|pre)>", following), (
            f"unescaped '<' near: ...{text[max(0, match.start() - 20):match.start() + 10]}..."
        )
    assert "&" not in re.sub(r"&(amp|lt|gt);", "", text), "unescaped '&'"


def _hand_built(**setup_kwargs) -> AnalysisResult:
    """A hand-built approved result — mirrors test_html_escaping.py's
    `_hand_built`, extended with the Phase 2 tp1/runner_tp/tier_star/
    tier_missed fields so each test controls the tier directly instead of
    depending on what the synthetic engine candles happen to classify."""
    result = AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.APPROVED_LIMIT,
        checked_at=datetime(2026, 8, 12, 7, 12, tzinfo=timezone.utc),
        price=3200.0,
        h4_trend=Trend.UP,
        price_decimals=2,
    )
    result.session_name = "New York"
    fvg = FVG(
        index=10, bottom=3100.0, top=3150.0, is_bullish=True,
        timestamp=datetime(2026, 8, 12, 7, 5, tzinfo=timezone.utc),
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


def _star_result(**setup_kwargs) -> AnalysisResult:
    kwargs = dict(
        tier_star=True, tp1=3175.0, runner_tp=3200.0, tier_missed=[],
    )
    kwargs.update(setup_kwargs)
    return _hand_built(**kwargs)


def _regular_result(**setup_kwargs) -> AnalysisResult:
    kwargs = dict(
        tier_star=False, tp1=3175.0, runner_tp=3200.0,
        tier_missed=["room", "pd"],
    )
    kwargs.update(setup_kwargs)
    return _hand_built(**kwargs)


class TestStarAlert:
    def test_star_header_present_when_tier_star(self):
        text = format_result(_star_result())
        assert "⭐" in text
        assert "<b>SNIPER</b>" in text

    def test_star_header_absent_when_not_tier_star(self):
        text = format_result(_regular_result())
        assert "⭐" not in text

    def test_tp1_and_runner_rendered_at_instrument_decimals(self):
        # USDJPY: 3 decimals (0.001, i.e. 1/10 pip) per instruments.py.
        result = _star_result(tp1=151.234, runner_tp=151.9)
        result.symbol = "USDJPY"
        result.price_decimals = 3
        text = format_result(result)
        assert "TP1 (half): 151.234" in text
        assert "runner: 151.900" in text

    def test_tp1_runner_also_rendered_for_non_star(self):
        """tp1/runner_tp are computed for every completed setup (engine.py,
        Task 2) — the star header is tier-gated, the levels are not."""
        text = format_result(_regular_result())
        assert "TP1 (half): 3175.00" in text
        assert "runner: 3200.00" in text

    def test_star_alert_is_valid_html_and_escapes_dynamic_values(self):
        """A crafted '<' in a dynamic value (here: an engine warning) must
        survive escaping in the star card, same discipline as every other
        dynamic string in this file (CLAUDE.md)."""
        result = _star_result()
        result.warnings = ["fill < 50% on the FVG"]
        text = format_result(result)
        _assert_valid_telegram_html(text)
        assert "fill &lt; 50%" in text

    def test_star_alert_carries_a_prague_timestamp(self):
        text = format_result(_star_result())
        assert "12.08 09:12 Prague" in text


class TestQuietSetup:
    def test_is_short(self):
        text = format_quiet_setup(_regular_result())
        assert len(text) < 400

    def test_lists_missed_conditions(self):
        text = format_quiet_setup(_regular_result(tier_missed=["room", "pd"]))
        assert "room" in text and "pd" in text
        assert "Missed for ⭐" in text

    def test_no_pre_block_or_ladder(self):
        text = format_quiet_setup(_regular_result())
        assert "<pre" not in text
        assert "🎯" not in text  # the ladder header never renders here

    def test_carries_pair_direction_entry_sl_tp1_runner(self):
        text = format_quiet_setup(_regular_result())
        assert "ETHUSD" in text and "LONG" in text
        assert "3125.00" in text  # entry
        assert "3050.00" in text  # SL
        assert "3175.00" in text  # tp1
        assert "3200.00" in text  # runner

    def test_carries_a_prague_timestamp(self):
        text = format_quiet_setup(_regular_result())
        assert "12.08 09:12 Prague" in text

    def test_crafted_angle_bracket_in_missed_condition_survives_escaping(self):
        result = _regular_result(tier_missed=["room", "<script>alert(1)</script>"])
        text = format_quiet_setup(result)
        _assert_valid_telegram_html(text)
        assert "<script>" not in text
        assert "&lt;script&gt;" in text

    def test_crafted_angle_bracket_in_symbol_survives_escaping(self):
        result = _regular_result()
        result.symbol = "ETH<script>USD"
        text = format_quiet_setup(result)
        _assert_valid_telegram_html(text)
        assert "<script>" not in text

    def test_empty_missed_list_renders_a_dash(self):
        text = format_quiet_setup(_regular_result(tier_missed=[]))
        assert "Missed for ⭐: —" in text
