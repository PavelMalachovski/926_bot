"""Regression tests: Telegram HTML must not be broken by dynamic text.

Production incident 2026-07-16: a /check summary containing the engine reason
"fill < 50%" was rejected by Telegram with `can't parse entities: Unsupported
start tag` because `<` was sent unescaped in parse_mode=HTML.
"""

import re
from datetime import datetime, timezone

from app.services.smc.liquidity import LiquidityLevel
from app.services.smc.models import AnalysisResult, Verdict
from app.services.smc.news import NewsCalendar, parse_feed
from app.services.smc.notifier import escape_html, format_no_setup, format_result, format_target


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
