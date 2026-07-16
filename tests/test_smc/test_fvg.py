"""Tests for FVG detection and validation (Rule 4)."""

from app.services.smc.fvg import (
    best_rejected_fvg,
    find_fvgs,
    measure_fill,
    select_valid_fvg,
)
from app.services.smc.models import Direction
from tests.test_smc.helpers import candle, m5_long_trigger


class TestDetection:
    def test_bullish_fvg_found(self):
        m5 = m5_long_trigger()
        fvgs = find_fvgs(m5, Direction.LONG, from_index=14)
        # Micro-gaps are detected too; the tradeable one is the first >= $2.
        fvg = next(f for f in fvgs if f.size >= 2.0)
        assert fvg.is_bullish
        assert fvg.bottom == 3136.0  # high of the oldest candle
        assert fvg.top == 3139.5  # low of the newest candle
        assert fvg.size == 3.5

    def test_bearish_fvg_found(self):
        candles = [
            candle(3150, 3151, 3145, 3146, index=0),
            candle(3146, 3147, 3138, 3139, index=1),
            candle(3139, 3140, 3133, 3134, index=2),  # high 3140 < low[0] 3145
        ]
        fvgs = find_fvgs(candles, Direction.SHORT, from_index=0)
        assert len(fvgs) == 1
        assert not fvgs[0].is_bullish
        assert fvgs[0].bottom == 3140.0
        assert fvgs[0].top == 3145.0

    def test_no_fvg_without_gap(self):
        candles = [
            candle(3130, 3135, 3128, 3134, index=0),
            candle(3134, 3138, 3132, 3137, index=1),
            candle(3137, 3140, 3134, 3139, index=2),  # low 3134 < high[0] 3135
        ]
        assert find_fvgs(candles, Direction.LONG, from_index=0) == []


class TestValidation:
    @staticmethod
    def _main_fvg(m5):
        fvgs = find_fvgs(m5, Direction.LONG, from_index=14)
        return next(f for f in fvgs if f.size >= 2.0)

    def test_untouched_fvg_has_zero_fill(self):
        m5 = m5_long_trigger()
        fvg = measure_fill(m5, self._main_fvg(m5))
        assert fvg.fill_pct == 0.0
        assert not fvg.closed_through

    def test_deep_retrace_fills_fvg(self):
        m5 = m5_long_trigger()
        # Wick down into the lower half of the gap (3136-3139.5).
        m5.append(candle(3150, 3150.5, 3137.0, 3145, index=len(m5)))
        fvg = measure_fill(m5, self._main_fvg(m5))
        assert fvg.fill_pct > 0.5

    def test_body_close_through_invalidates(self):
        m5 = m5_long_trigger()
        m5.append(candle(3145, 3146, 3130, 3132, index=len(m5)))  # closes below 3136
        fvg = measure_fill(m5, self._main_fvg(m5))
        assert fvg.closed_through

    def test_select_valid_fvg_respects_min_size(self):
        m5 = m5_long_trigger()
        assert select_valid_fvg(m5, Direction.LONG, 14, min_size=2.0) is not None
        assert select_valid_fvg(m5, Direction.LONG, 14, min_size=5.0) is None

    def test_select_rejects_filled_fvg(self):
        m5 = m5_long_trigger()
        m5.append(candle(3150, 3150.5, 3136.5, 3145, index=len(m5)))  # >50% fill
        assert select_valid_fvg(m5, Direction.LONG, 14, min_size=2.0) is None


class TestRejectionDiagnostics:
    def test_reports_undersized_candidate(self):
        m5 = m5_long_trigger()
        rejected = best_rejected_fvg(m5, Direction.LONG, 14, min_size=5.0)
        assert rejected is not None
        fvg, problems = rejected
        assert problems == ["size"]
        assert fvg.size == 3.5  # the largest gap of the impulse

    def test_reports_fill_when_size_is_fine(self):
        m5 = m5_long_trigger()
        m5.append(candle(3150, 3150.5, 3136.5, 3145, index=len(m5)))  # 86% fill
        rejected = best_rejected_fvg(m5, Direction.LONG, 14, min_size=2.0)
        fvg, problems = rejected
        assert "fill" in problems and "size" not in problems

    def test_none_when_no_gap_exists(self):
        flat = [
            candle(3130, 3135, 3128, 3134, index=0),
            candle(3134, 3138, 3132, 3137, index=1),
            candle(3137, 3140, 3134, 3139, index=2),
        ]
        assert best_rejected_fvg(flat, Direction.LONG, 0, min_size=2.0) is None
