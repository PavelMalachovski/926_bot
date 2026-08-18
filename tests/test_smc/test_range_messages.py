"""Range-mode messages: the box, the target and the vocabulary.

Two defects from the whole-branch review of 2026-08-18 are pinned here.

1. The range target was INVISIBLE. `_format_detector_alert` gated its
   take-profit line on `setup.target is not None`, and a range setup has no
   liquidity target by design (its objective is the opposite boundary), so
   neither the TP nor its RR ever reached the owner. On the quiet tier —
   where most range setups land, since the ⭐ needs a sweep — there was no
   TP line at all. He was told to trade a range and shown neither the range
   nor its target.

2. Five surfaces still called a boundary an "H1 Supply/Demand zone".
   `TestNoSurfaceCallsABoundaryAnH1Zone` sweeps every range-mode message the
   bot can emit, so the next surface added cannot quietly regress it.

Every range fixture here is a real `TripleSyncEngine.evaluate()` /
`build_plan()` run on the synthetic candles `test_range_engine.py` and
`test_range_plan.py` already prove out — nothing is mocked.
"""

import re
from datetime import datetime, timezone

from app.services.smc.liquidity import LiquidityLevel
from app.services.smc.models import (
    AnalysisResult, Direction, FVG, TradeSetup, Trend, Verdict, Zone,
)
from app.services.smc.notifier import (
    format_plan,
    format_plan_summary,
    format_quiet_setup,
    format_result,
    format_zone_alert,
)
from tests.test_smc.test_range_engine import (
    _run,
    h1_ranging_swept,
    m5_at_bottom,
    m5_at_top,
    m5_at_top_pierced,
    m5_mid_range,
)
from tests.test_smc.test_range_plan import _build

RANGE_TOP = 3200.8
RANGE_BOTTOM = 3179.2


def _assert_valid_telegram_html(text: str):
    """The same narrow contract as test_html_escaping.py: only <b>/<pre>
    tags, everything else must have gone through escape_html."""
    for match in re.finditer(r"<", text):
        following = text[match.start():match.start() + 6]
        assert re.match(r"</?(b|pre)>", following), (
            f"unescaped '<' near: "
            f"...{text[max(0, match.start() - 20):match.start() + 10]}..."
        )
    assert "&" not in re.sub(r"&(amp|lt|gt);", "", text), "unescaped '&'"


def _star_range_result() -> AnalysisResult:
    """A SHORT at the range high that swept the boundary — the ⭐ tier."""
    result = _run(h1=h1_ranging_swept(3204.0), m5=m5_at_top_pierced(3204.0))
    assert result.setup.tier_star is True
    return result


def _regular_range_result() -> AnalysisResult:
    """The same boundary without the sweep — the quiet tier, which is where
    most range setups land."""
    result = _run()
    assert result.setup.tier_star is False
    return result


class TestTheRangeTargetIsVisible:
    """Finding 2: the objective and its RR must reach the owner on BOTH
    tiers, and so must the box he is trading between."""

    def test_star_card_names_the_box(self):
        text = format_result(_star_range_result())
        assert f"📦 Range box            {RANGE_BOTTOM:.2f} – {RANGE_TOP:.2f}" in text

    def test_star_card_names_the_target_and_its_rr(self):
        result = _star_range_result()
        text = format_result(result)
        assert f"🎯 Range LOW target     {result.setup.take_profit:.2f}" in text
        assert f"1:{result.setup.rr:.1f}" in text

    def test_the_long_side_targets_the_range_high(self):
        result = _run(m5=m5_at_bottom())
        text = format_result(result)
        assert f"🎯 Range HIGH target    {result.setup.take_profit:.2f}" in text
        assert result.setup.take_profit == 3198.80

    def test_the_star_card_shows_no_hybrid_levels(self):
        """D14: a range setup has none to show."""
        text = format_result(_star_range_result())
        assert "TP1 (half)" not in text
        assert "runner:" not in text

    def test_quiet_setup_carries_the_box_the_target_and_the_rr(self):
        result = _regular_range_result()
        text = format_quiet_setup(result)
        assert f"range {RANGE_BOTTOM:.2f}–{RANGE_TOP:.2f}" in text
        assert f"TP {result.setup.take_profit:.2f}" in text
        assert f"(1:{result.setup.rr:.1f})" in text

    def test_quiet_setup_shows_no_hybrid_placeholders(self):
        """It used to print "TP1 n/a · runner n/a" for a range setup."""
        text = format_quiet_setup(_regular_range_result())
        assert "TP1" not in text and "runner" not in text
        assert "n/a" not in text

    def test_both_tiers_are_valid_telegram_html(self):
        _assert_valid_telegram_html(format_result(_star_range_result()))
        _assert_valid_telegram_html(format_quiet_setup(_regular_range_result()))


class TestNoSurfaceCallsABoundaryAnH1Zone:
    """Finding 6: a boundary is not an H1 Supply/Demand zone. Every
    range-mode message the bot can emit is swept here — add a surface, and
    it has to be added to `_messages` to stay covered."""

    FORBIDDEN = ("H1 Supply zone", "H1 Demand zone")

    def _messages(self):
        """(name, text) for every range-mode message the bot emits."""
        star = _star_range_result()
        regular = _regular_range_result()
        watch = _run(m5=m5_at_top()[:10])          # at the boundary, no CHoCH
        mid = _run(m5=m5_mid_range())              # box live, nothing to trade
        plan = _build()
        short_scenario = next(
            s for s in plan.scenarios if s.direction == Direction.SHORT
        )
        long_scenario = next(
            s for s in plan.scenarios if s.direction == Direction.LONG
        )
        return [
            ("star alert", format_result(star)),
            ("quiet alert", format_quiet_setup(regular)),
            ("watch card", format_result(watch)),
            ("mid-range card", format_result(mid)),
            ("plan", format_plan(plan)),
            ("plan summary", format_plan_summary("07:55", [plan])),
            ("zone alert (short)",
             format_zone_alert("ETHUSD", short_scenario, 2)),
            ("zone alert (long)",
             format_zone_alert("ETHUSD", long_scenario, 2)),
            ("engine reasons", " ".join(
                watch.reasons + watch.watch_notes + mid.reasons
                + mid.watch_notes
            )),
        ]

    def test_no_range_message_calls_a_boundary_an_h1_zone(self):
        for name, text in self._messages():
            for phrase in self.FORBIDDEN:
                assert phrase not in text, f"{name} still says {phrase!r}"

    def test_the_surfaces_name_the_boundary_instead(self):
        named = {name: text for name, text in self._messages()}
        assert "📍 Range HIGH boundary" in named["star alert"]
        assert "<b>Range HIGH boundary:</b>" in named["watch card"]
        assert "Range HIGH boundary" in named["plan"]
        assert "Range LOW boundary" in named["plan"]

    def test_the_plan_footer_anchors_the_stop_to_the_boundary(self):
        text = format_plan(_build())
        assert "SL is preliminary (beyond the range boundary)" in text
        assert "beyond the H1 zone" not in text

    def test_the_stop_line_does_not_claim_swept_liquidity(self):
        """In range mode Rule 6 takes the further of the swept wick and the
        boundary, so on an untouched boundary nothing was swept at all."""
        result = _regular_range_result()
        text = format_result(result)
        assert "Swept liquidity" not in text
        assert f"🛑 Stop reference       {RANGE_TOP:.2f}" in text


class TestTrendMessagesAreByteIdentical:
    """The other half of Findings 2/6: none of it may touch a trend setup.
    Both goldens were captured from the pre-fix `notifier.py` (git HEAD
    b280906) and compared byte for byte."""

    @staticmethod
    def _trend_result() -> AnalysisResult:
        ts = datetime(2026, 8, 12, 7, 12, tzinfo=timezone.utc)
        result = AnalysisResult(
            symbol="ETHUSD",
            verdict=Verdict.APPROVED_LIMIT,
            checked_at=ts,
            price=3200.0,
            h4_trend=Trend.UP,
            h1_trend=Trend.UP,
            price_decimals=2,
        )
        result.session_name = "New York"
        result.warnings = ["price has run 1.2R past the imbalance"]
        result.funding_rate = 0.0002
        result.h1_zone = Zone(
            bottom=3100.0, top=3140.0, is_demand=True, pivot_index=7,
            timestamp=ts, touches=0,
        )
        result.setup = TradeSetup(
            direction=Direction.LONG,
            entry=3125.0,
            stop_loss=3050.0,
            take_profit=3300.0,
            rr=2.33,
            fvg=FVG(
                index=10, bottom=3100.0, top=3150.0, is_bullish=True,
                timestamp=ts, fill_pct=0.2,
            ),
            lot_hint="0.1234 ETH (risk $200.00 = 2.0% of $10000 deposit)",
            target=LiquidityLevel(
                price=3305.0, is_high=True, timeframe="H1", timestamp=ts,
                equal_count=2,
            ),
            order_block=Zone(
                bottom=3105.0, top=3118.0, is_demand=True, pivot_index=9,
                timestamp=ts,
            ),
            ladder=[
                LiquidityLevel(price=3305.0, is_high=True, timeframe="H1",
                               timestamp=ts, equal_count=2),
                LiquidityLevel(price=3400.0, is_high=True, timeframe="H4",
                               timestamp=ts, equal_count=1),
            ],
            zones_ahead=[
                Zone(bottom=3010.0, top=3040.0, is_demand=True, pivot_index=3,
                     timestamp=ts, touches=0),
            ],
            tp1=3275.0,
            runner_tp=3350.0,
            tier_star=True,
            tier_missed=[],
        )
        return result

    GOLDEN_ALERT = "\n".join([
        "🚨 <b>SETUP READY — ETHUSD · LONG</b> · H4 uptrend",
        "H4 up · H1 up",
        "⭐ <b>SNIPER</b>",
        "   from this morning's plan",
        "",
        "📍 H1 Demand zone (OB)  3100.00 – 3140.00",
        "⚡ M5 imbalance (FVG)   3100.00 – 3150.00   ← limit order (3125.00)",
        "🧱 M5 order block       3105.00 – 3118.00   ← deeper entry (3118.00)",
        "🛑 Swept liquidity      3052.00   ← stop behind the wick "
        "(3050.00 with buffer)",
        "🔫 TP1 (half): 3275.00 · runner: 3350.00",
        "",
        "🎯 Unswept liquidity ahead      RR from FVG / from OB",
        "<pre>",
        "     3305.00      $180.00   1:2.4 / 1:2.7   H1 · EQH x2",
        "     3400.00      $275.00   1:3.6 / 1:4.1   H4",
        "</pre>",
        "",
        "🧱 Untested zones further out   ← deeper entries",
        "     3010.00 – 3040.00   (OB · 0 touches)",
        "",
        "   ⚠️ price has run 1.2R past the imbalance",
        "",
        "   ref · FVG 50.00, 20% filled · New York",
        "   ref · tracked objective 3300.00 (1:2.3) · "
        "H1 swing high 3305.00 (EQH x2)",
        "   ref · size 0.1234 ETH (risk $200.00 = 2.0% of $10000 deposit)",
        "   ref · funding 0.020%/8h",
        "   ref · a pending order expires with this session (Rule 10)",
        "   ref · 12.08 09:12 Prague · price 3200.00",
    ])

    GOLDEN_QUIET = (
        "🔹 <b>ETHUSD LONG</b> · entry 3125.00 · SL 3050.00 · "
        "TP1 3275.00 · runner 3350.00\n"
        "Missed for ⭐: room, pd · 12.08 09:12 Prague"
    )

    def test_the_trend_alert_is_unchanged(self):
        text = format_result(self._trend_result(), in_plan=True)
        assert text == self.GOLDEN_ALERT

    def test_the_trend_quiet_line_is_unchanged(self):
        result = self._trend_result()
        result.setup.tier_star = False
        result.setup.tier_missed = ["room", "pd"]
        assert format_quiet_setup(result) == self.GOLDEN_QUIET
