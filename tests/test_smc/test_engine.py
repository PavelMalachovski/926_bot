"""End-to-end tests for the TripleSyncEngine checklist."""

from datetime import datetime, timezone

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.profiles import AGGRESSIVE, CONSERVATIVE
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    candle,
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


def _agg_engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(min_fvg_size=2.0, sl_buffer=2.0, min_rr=2.0, profile=AGGRESSIVE)
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


class TestProfiles:
    def test_result_carries_profile_key(self):
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=_fresh_result(),
        )
        assert result.profile_key == "conservative"

    def test_aggressive_takes_direction_from_h4_choch_when_flat(self):
        # H4 structure: a real HH+HL leg (shared prefix with H4_UPTREND_CLOSES)
        # followed by a decline that breaks below the last confirmed low and
        # never reclaims it. Only one low pivot ever confirms (the decline is
        # monotonic), so detect_trend() sees < 2 confirmed lows -> FLAT; but
        # h4_choch_direction() still finds the unreclaimed break-down -> SHORT.
        # (Hand-verified: the brief's own closes list produces zero confirmed
        # pivots at all — tied fractal highs/lows — so h4_choch_direction()
        # returns None there. This data was substituted to actually exercise
        # the aggressive H4-CHoCH path; see task-5-report.md.)
        closes = [
            3000, 3020, 3040, 3060, 3080, 3100, 3090, 3075, 3060, 3050,
            3070, 3100, 3140, 3170, 3200, 3185, 3160, 3140, 3120,
            3100, 3080, 3050, 3000, 2950,
        ]
        h4 = make_candles(closes, step_minutes=240)
        # conservative: FLAT -> SKIP
        cons = _engine().evaluate(
            h4=h4, h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(), result=_fresh_result(),
        )
        assert cons.verdict == Verdict.SKIP
        assert "no direction" in " ".join(cons.reasons).lower()
        # aggressive: consults h4_choch_direction (no crash, direction resolved)
        agg = _agg_engine().evaluate(
            h4=h4, h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(), result=_fresh_result(),
        )
        assert agg.profile_key == "aggressive"
        # with a downward CHoCH the aggressive path no longer SKIPs on "no direction"
        assert "no direction" not in " ".join(agg.reasons).lower()


class TestCryptoSameDayFallbackSurvivesProfileWiring:
    def test_crypto_conservative_still_accepts_cross_session_fvg(self):
        """Regression for the crypto same_day fallback (`or source ==
        "crypto"`). CONSERVATIVE has fvg_day_scope=False, so without the
        fallback the engine would fall back to same_session and reject an
        FVG that formed in a different Prague session window than "now".

        Full end-to-end evaluate(): same OHLC shape as m5_long_trigger()
        (proven to APPROVE with entry=3139.5 at the earliest FVG, index 15),
        but shifted so index 15 (13:50 Prague, Frankfurt/London window) and
        index 19 (14:10 Prague, New York window) straddle the 14:00 session
        boundary while staying on the same Prague calendar day.

        If the crypto fallback were missing, same_session would reject the
        index-15 FVG and the engine would instead pick the later index-17
        FVG (entry 3147.0) that shares "now"'s session -- so pinning the
        exact entry price proves the same_day (not same_session) scope won.
        """
        spec = [
            (3160.0, 3161.0, 3155.0, 3156.0),
            (3156.0, 3157.0, 3151.0, 3152.0),
            (3152.0, 3153.0, 3147.0, 3148.0),
            (3148.0, 3149.0, 3143.0, 3144.0),
            (3144.0, 3145.0, 3140.0, 3142.0),
            (3142.0, 3146.0, 3141.0, 3145.0),
            (3145.0, 3148.0, 3144.0, 3147.0),
            (3147.0, 3147.5, 3141.0, 3142.0),
            (3142.0, 3143.0, 3137.0, 3138.0),
            (3138.0, 3139.0, 3133.0, 3134.0),
            (3134.0, 3135.0, 3130.0, 3131.0),
            (3131.0, 3133.0, 3130.5, 3132.0),
            (3132.0, 3134.0, 3131.0, 3133.0),
            (3133.0, 3136.0, 3132.0, 3135.5),
            (3135.5, 3141.0, 3135.0, 3140.5),
            (3140.5, 3144.0, 3139.5, 3143.0),
            (3143.0, 3149.5, 3142.0, 3149.0),
            (3149.0, 3151.0, 3147.0, 3150.0),
            (3150.0, 3152.0, 3148.0, 3151.0),
            (3151.0, 3152.0, 3149.0, 3150.0),
        ]
        cross_session_start = datetime(2026, 7, 6, 10, 35, tzinfo=timezone.utc)
        m5 = [
            candle(*row, index=i, start=cross_session_start)
            for i, row in enumerate(spec)
        ]

        engine = _engine(instrument=get_instrument("ETHUSD"), profile=CONSERVATIVE)
        result = engine.evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5,
            result=_fresh_result(),
        )
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup.entry == 3139.5  # earliest FVG (index 15), only
        # reachable via same_trading_day scope, not same_session
