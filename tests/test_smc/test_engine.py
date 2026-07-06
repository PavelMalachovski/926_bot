"""End-to-end tests for the TripleSyncEngine checklist."""

from datetime import datetime, timezone

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)


def _fresh_result() -> AnalysisResult:
    return AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )


def _engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(min_fvg_size=2.0, sl_buffer=2.0, min_rr=2.0)
    defaults.update(kwargs)
    return TripleSyncEngine(**defaults)


class TestApprovedSetup:
    def test_full_bullish_setup_approved(self):
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.h4_trend == Trend.UP
        setup = result.setup
        assert setup.direction == Direction.LONG
        assert setup.entry == 3139.5  # top of the bullish FVG
        assert setup.stop_loss == 3128.0  # pivot low 3130 - $2 buffer
        assert setup.take_profit == 3200.0  # proximal edge of H1 supply
        assert setup.rr >= 2.0
        assert not setup.entry_is_market  # last close 3150 is above the FVG

    def test_lot_hint_computed_from_deposit(self):
        result = _engine(deposit=1000.0, risk_pct=2.0).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.setup.lot_hint is not None
        assert "$20.00" in result.setup.lot_hint  # 2% of $1000


class TestSkipsAndWatch:
    def test_flat_h4_skips(self):
        closes = [3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000, 3050, 3000]
        result = _engine().evaluate(
            h4=make_candles(closes, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.SKIP
        assert result.h4_trend == Trend.FLAT

    def test_watch_when_price_has_not_reached_zone(self):
        # M5 stays far above the demand zone: pullback phase, no entry.
        m5 = make_candles([3180, 3178, 3176, 3175, 3174, 3175, 3176, 3175, 3174, 3173])
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5,
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.WATCH
        assert result.setup is None

    def test_watch_when_no_choch_yet(self):
        m5 = m5_long_trigger()[:16]  # in the zone but no structure break yet
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5,
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.WATCH

    def test_skip_when_rr_too_low(self):
        result = _engine(min_rr=10.0).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.SKIP
        assert any("RR" in r for r in result.reasons)
