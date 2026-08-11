"""End-to-end tests for the TripleSyncEngine checklist."""

from datetime import datetime, timezone

from app.core.config import SMCSettings
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.profiles import AGGRESSIVE, CONSERVATIVE
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H1_RALLY_INTO_SUPPLY_CLOSES,
    H4_DOWNTREND_CLOSES,
    H4_UPTREND_CLOSES,
    candle,
    m5_long_trigger,
    m5_long_trigger_deep_sweep,
    m5_short_trigger_deep_sweep,
    make_candles,
)


def _fresh_result() -> AnalysisResult:
    return AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.SKIP,
        checked_at=datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc),
    )


def _engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0
    )
    defaults.update(kwargs)
    return TripleSyncEngine(**defaults)


def _agg_engine(**kwargs) -> TripleSyncEngine:
    defaults = dict(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0,
        profile=AGGRESSIVE,
    )
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
        # TP one buffer short of the H1 swing high liquidity at 3221.0
        # (risk = 3139.5 - 3128.0 = 11.5; rr = (3219.0-3139.5)/11.5 = 6.91)
        assert setup.take_profit == 3219.0
        assert setup.target.price == 3221.0
        assert setup.rr == 6.91
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
        # H1 (H1_PULLBACK_CLOSES) is FLAT here too (only one confirmed low
        # pivot, see TestH1DirectionFallback below) — confirms the CHoCH
        # branch is still reached, and labelled, when H1 gives no fallback.
        assert agg.direction_source == "h4_choch"


# H1_PULLBACK_CLOSES (see helpers.py) forms exactly one confirmed low pivot
# (index 7, the demand pivot at 3132/low 3131.0 that m5_long_trigger_deep_sweep
# is built against) — detect_trend() needs two confirmed lows to call a trend,
# so H1_PULLBACK_CLOSES alone reads FLAT, not UP. Verified directly:
# detect_trend(make_candles(H1_PULLBACK_CLOSES, step_minutes=60)) is
# Trend.FLAT. The brief's suggested test reuses H1_PULLBACK_CLOSES as-is for
# the H1 side, which would make test_flat_h4_takes_direction_from_h1 fail for
# the wrong reason (H1 has no trend either) rather than exercising the H1
# fallback. This fixture prepends an earlier, lower down-up leg — closes
# [3140, 3125, 3105, 3080, 3095, 3110, 3100] — that confirms a low pivot at
# 3079.0, strictly below the 3131.0 pivot. The prefix's last close (3100)
# equals H1_PULLBACK_CLOSES[0], so every candle from index 7 onward (the
# demand pivot, the zone, the untested state) is byte-for-byte identical to
# the plain H1_PULLBACK_CLOSES case — only shifted in index — so
# m5_long_trigger_deep_sweep's zone (3131.0-3138.0) still matches. Verified:
# find_pivots gives lows [(index 3, 3079.0), (index 14, 3131.0)] and highs
# [(index 11, 3151.0), (index 19, 3221.0)] -> HL and HH -> detect_trend is
# Trend.UP; find_h1_zone(..., Direction.LONG, max_touches=0) still returns
# bottom=3131.0, top=3138.0, touches=0, invalidated=False.
H1_UPTREND_FOR_FALLBACK_CLOSES = [
    3140, 3125, 3105, 3080, 3095, 3110, 3100,
] + H1_PULLBACK_CLOSES


class TestH1DirectionFallback:
    """When H4 has no clean structure but H1 does, announce with H1's
    direction (owner decision 2026-08-06). Measured at about +40% announced
    setups, not a multiple."""

    def test_flat_h4_takes_direction_from_h1(self):
        # H4 flat: alternating closes give zero confirmed fractal pivots (the
        # local highs/lows tie with their same-parity neighbours two candles
        # away, failing the strict-inequality pivot test), so detect_trend()
        # sees < 2 highs/lows -> Trend.FLAT trivially, and h4_choch_direction()
        # (which also needs a pivot to anchor a break) returns None. Verified
        # directly: find_pivots() on this series is []; detect_trend() is
        # Trend.FLAT; h4_choch_direction() is None.
        h4 = make_candles(
            [3000, 3100, 3000, 3100, 3000, 3100, 3000] * 4, step_minutes=240
        )
        result = _engine().evaluate(
            h4=h4,
            h1=make_candles(H1_UPTREND_FOR_FALLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=_fresh_result(),
        )
        assert result.h4_trend is Trend.FLAT
        assert result.direction_source == "h1"

    def test_clean_h4_still_wins(self):
        result = _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=_fresh_result(),
        )
        assert result.direction_source == "h4"

    def test_h1_fallback_applies_to_aggressive_profile_too(self):
        # The H1 fallback is not a profile decision point (resolved
        # ambiguity, task-3 brief) — it applies before the aggressive-only
        # CHoCH branch is even considered, on both profiles.
        h4 = make_candles(
            [3000, 3100, 3000, 3100, 3000, 3100, 3000] * 4, step_minutes=240
        )
        result = _agg_engine().evaluate(
            h4=h4,
            h1=make_candles(H1_UPTREND_FOR_FALLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=_fresh_result(),
        )
        assert result.direction_source == "h1"

    def test_choch_direction_none_does_not_label_source_h4_choch(self):
        # H4 flat (zero confirmed pivots, see the fixture comment above) and
        # H1 flat (H1_PULLBACK_CLOSES — established above to read Trend.FLAT
        # on its own), on the aggressive profile: h1_trend is FLAT so the
        # chain reaches h4_choch_direction(h4), which returns None (no pivot
        # to anchor a break). direction stays None -> SKIP. Pins that
        # direction_source is NOT stamped "h4_choch" in this case -- the two
        # must never disagree (review finding, task-3).
        h4 = make_candles(
            [3000, 3100, 3000, 3100, 3000, 3100, 3000] * 4, step_minutes=240
        )
        result = _agg_engine().evaluate(
            h4=h4, h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=_fresh_result(),
        )
        assert result.verdict == Verdict.SKIP
        assert result.direction_source != "h4_choch"
        assert result.direction_source == "h4"


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


class TestSweepStop:
    """Rule 6 (2026-08-05): SL sits behind the swept extreme, not behind the
    last fractal pivot."""

    def test_stop_is_below_the_sweep_not_the_later_pivot(self):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        result = _engine().evaluate(
            h4=h4, h1=h1, m5=m5_long_trigger_deep_sweep(), result=_fresh_result()
        )
        assert result.setup is not None, result.reasons
        # sweep low 3126.0 minus the $2 buffer, NOT 3132.5 - 2 = 3130.5
        assert result.setup.stop_loss == 3124.0


class TestLiquidityTarget:
    """Rule 7 (2026-08-05): TP is the nearest unswept liquidity, one buffer
    short of the level; RR is computed, not assigned."""

    def _run(self, **kwargs):
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        return _engine(**kwargs).evaluate(
            h4=h4, h1=h1, m5=m5_long_trigger_deep_sweep(), result=_fresh_result()
        )

    def test_tp_is_the_nearest_unswept_liquidity_minus_buffer(self):
        result = self._run()
        assert result.setup is not None, result.reasons
        # H1 swing high 3221.0 is the nearest unswept pool above entry 3140.5
        assert result.setup.take_profit == 3219.0
        assert result.setup.target.price == 3221.0
        assert result.setup.target.timeframe == "H1"

    def test_rr_is_computed_from_the_target(self):
        result = self._run()
        # entry 3140.5, SL 3124.0 -> risk 16.5; TP 3219.0 -> 78.5 / 16.5
        assert result.setup.rr == 4.76

    def test_rr_does_not_track_the_threshold(self):
        # The old rule assigned rr = the configured multiple, so the RR
        # moved with the setting.
        assert self._run(min_rr=1.0).setup.rr == self._run(min_rr=2.0).setup.rr

    def test_setup_below_the_threshold_is_announced_with_a_warning(self):
        # Detector mode (2026-08-06): min_rr no longer suppresses. The RR is
        # still measured (1:4.8 against a 1:8 threshold) and now travels as a
        # warning instead of killing the announcement.
        result = self._run(min_rr=8.0)
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup is not None
        assert result.setup.rr == 4.76
        assert "1:4.8" in " ".join(result.warnings)

    def test_short_tp_is_on_the_correct_side_of_entry(self):
        # SHORT mirror of m5_long_trigger_deep_sweep (see helpers.py and
        # task-3-report.md finding 1 for the K=6270 reflection and the
        # independent re-derivation): entry 3129.5, SL 3146.0, target the H1
        # swing low at 3049.0, TP = 3049.0 + $2 buffer = 3051.0,
        # rr = (3129.5 - 3051.0) / (3146.0 - 3129.5) = 78.5 / 16.5 = 4.76.
        h4 = make_candles(H4_DOWNTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_RALLY_INTO_SUPPLY_CLOSES, step_minutes=60)
        result = _engine().evaluate(
            h4=h4, h1=h1, m5=m5_short_trigger_deep_sweep(), result=_fresh_result()
        )
        assert result.setup is not None, result.reasons
        setup = result.setup
        assert setup.direction == Direction.SHORT
        assert setup.take_profit < setup.entry < setup.stop_loss
        assert setup.take_profit == 3051.0
        assert setup.entry == 3129.5
        assert setup.stop_loss == 3146.0
        assert setup.target.price == 3049.0
        assert setup.target.is_high is False
        assert setup.rr == 4.76

    def test_target_inside_the_sl_buffer_yields_no_take_profit(self):
        # An inflated sl_buffer pulls the target within one buffer of entry:
        # TP = 3221.0 - 100 = 3121.0, BELOW entry 3140.5 for a LONG — a
        # negative reward that abs() alone would report as a positive RR.
        # Detector mode announces the setup anyway, but refuses to invent an
        # objective: no TP, no target, rr 0.0. min_rr=0.1 proves the cleared
        # take-profit does not depend on the threshold.
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        result = _engine(sl_buffer=100.0, min_rr=0.1).evaluate(
            h4=h4, h1=h1, m5=m5_long_trigger_deep_sweep(), result=_fresh_result()
        )
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup is not None
        assert result.setup.take_profit is None
        assert result.setup.target is None
        assert result.setup.rr == 0.0
        assert "inside the stop buffer" in " ".join(result.warnings)


class TestEntryStalenessGate:
    """Rule 5.1 (2026-08-05): a limit far below market rarely fills. Since
    2026-08-06 (detector mode) the measurement is a warning, not a skip —
    the owner sets his own entry — but the arithmetic is unchanged."""

    def _long(self, price, **kwargs):
        result = _fresh_result()
        result.price = price
        return _engine(**kwargs).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(),
            result=result,
        )

    def test_long_warned_when_price_has_run_past_the_entry(self):
        # entry 3140.5, risk 16.5 -> at 0.5R the limit may sit at most
        # 8.25 above market; 3160.0 is 19.5 away (1.18R). Announced, labelled.
        result = self._long(3160.0, max_entry_gap_r=0.5)
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup is not None
        assert "1.2R" in " ".join(result.warnings)

    def test_long_approved_without_a_warning_when_price_is_near_the_entry(self):
        result = self._long(3145.0, max_entry_gap_r=0.5)
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup is not None
        assert result.warnings == []

    def test_gate_is_silent_for_a_market_entry(self):
        # price inside the FVG (3138.0-3140.5) is at or below the entry, so
        # the gap is never positive and the tightest threshold cannot fire.
        result = self._long(3139.0, max_entry_gap_r=0.0)
        assert result.verdict == Verdict.APPROVED_MARKET
        assert result.warnings == []

    def test_threshold_is_configurable(self):
        # A wide enough threshold removes the warning entirely.
        result = self._long(3160.0, max_entry_gap_r=2.0)
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.warnings == []

    def test_short_gate_measures_the_other_direction(self):
        # Mirror of the long case on the K=6270 reflected fixture: entry
        # 3129.5, risk 16.5. Price BELOW the entry is the run-away side.
        # gap = 3129.5 - 3110.0 = 19.5 -> 19.5 / 16.5 = 1.18 -> "1.2R",
        # same arithmetic as the long case above.
        result = _fresh_result()
        result.price = 3110.0
        out = _engine(max_entry_gap_r=0.5).evaluate(
            h4=make_candles(H4_DOWNTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_RALLY_INTO_SUPPLY_CLOSES, step_minutes=60),
            m5=m5_short_trigger_deep_sweep(),
            result=result,
        )
        assert out.verdict == Verdict.APPROVED_LIMIT
        assert out.setup is not None
        assert "1.2R" in " ".join(out.warnings)

    def test_shipped_default_is_0_75r(self):
        # Pins the production default (config.py's SMCSettings and the
        # engine constructor both ship 0.75) against a future edit silently
        # moving it — every other test in this class passes an explicit
        # value, so nothing else would catch that.
        # 0.75 was picked by the 59-day ETHUSD threshold sweep
        # (.superpowers/sdd/2026-08-05-liquidity-tp-sl/gate-sweep-report.md):
        # it is the knee of the fill-rate/alert-count curve, and the
        # provisional 0.5 produced zero conservative alerts in 59 days.
        assert TripleSyncEngine().max_entry_gap_r == 0.75
        assert SMCSettings().max_entry_gap_r == 0.75


class TestGatesAreNowLabels:
    """Detector mode (2026-08-06): a completed setup is always announced; the
    thresholds annotate rather than suppress."""

    def _run(self, price=None, **kwargs):
        result = _fresh_result()
        if price is not None:
            result.price = price
        return _engine(**kwargs).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(), result=result,
        )

    def test_low_rr_is_announced_with_a_warning(self):
        result = self._run(min_rr=8.0)
        assert result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET)
        assert result.setup is not None
        assert any("RR" in w for w in result.warnings)

    def test_stale_entry_is_announced_with_a_warning(self):
        result = self._run(price=3160.0, max_entry_gap_r=0.5)
        assert result.setup is not None
        assert any("past the imbalance" in w for w in result.warnings)
        assert "1.2R" in " ".join(result.warnings)

    def test_clean_setup_has_no_warnings(self):
        result = self._run(min_rr=1.0, max_entry_gap_r=99.0)
        assert result.setup is not None
        assert result.warnings == []

    def test_setup_carries_the_order_block_and_both_ladders(self):
        setup = self._run().setup
        assert setup.order_block is not None
        assert setup.order_block.top < setup.entry, "OB is a deeper long entry"
        # ...and a deeper entry that is still a trade: past the stop, the
        # ladder's "RR from OB" column would be computed on a collapsed risk.
        assert setup.order_block.top > setup.stop_loss, "OB is inside the stop"
        assert 1 <= len(setup.ladder) <= 5
        assert setup.ladder[0].price == setup.target.price, \
            "the ladder starts at the nearest pool"

    def test_zone_ladder_shows_deeper_entries_not_the_zone_being_entered(self):
        """Owner confirmation 2026-08-06: the zone ladder is alternative
        deeper entries on the trade's own side, and never the live zone the
        setup just formed in (which qualifies on price alone).

        The H1 fixture is extended with an earlier, deeper untested demand
        zone (3039-3060) so there is something to show: the shipped
        H1_PULLBACK_CLOSES has exactly one untested demand zone, the live
        one, and would render an empty block either way.
        """
        result = _fresh_result()
        result = _engine(max_entry_gap_r=99.0).evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(
                [3100, 3060, 3040, 3070, 3100] + H1_PULLBACK_CLOSES,
                step_minutes=60,
            ),
            m5=m5_long_trigger_deep_sweep(), result=result,
        )
        setup = result.setup
        assert setup is not None
        assert [(z.bottom, z.top) for z in setup.zones_ahead] == [(3039.0, 3060)]
        # the live zone is not a rung ...
        assert all(
            (z.bottom, z.top) != (result.h1_zone.bottom, result.h1_zone.top)
            for z in setup.zones_ahead
        )
        # ... and neither is the supply zone at 3200-3221 above the entry,
        # which is an obstacle on the way to target, not an entry.
        assert all(z.is_demand and z.top < setup.entry for z in setup.zones_ahead)

    def test_no_liquidity_ahead_is_announced_without_a_take_profit(self, monkeypatch):
        import app.services.smc.engine as E
        monkeypatch.setattr(E, "nearest_liquidity", lambda *a, **k: None)
        monkeypatch.setattr(E, "liquidity_ladder", lambda *a, **k: [])
        result = self._run()
        assert result.setup is not None
        assert result.setup.take_profit is None
        assert result.setup.target is None
        assert any("no unswept liquidity" in w.lower() for w in result.warnings)

    def test_target_inside_the_stop_buffer_clears_the_take_profit(self):
        # An inflated sl_buffer pulls the target within one buffer of entry:
        # TP = 3221.0 - 100 = 3121.0, BELOW entry 3140.5 for a LONG — a
        # negative reward that abs() alone would report as a positive RR.
        # The setup is still announced, but with no objective at all.
        result = self._run(sl_buffer=100.0, min_rr=0.1)
        assert result.setup is not None
        assert result.setup.take_profit is None
        assert result.setup.target is None
        assert result.setup.rr == 0.0
        assert any("stop buffer" in w for w in result.warnings)


class TestFundingAdvisory:
    """Rule 9.3 (advisory only, review-hardening Task 6). The danger tier
    used to fire only for LONG — a SHORT paying heavy negative funding (the
    symmetric squeeze case) fell through to the `elif abs(rate) > WARN`
    branch, whose text hardcoded 'is in the 0.05-0.1% zone' even when the
    rate was well outside that band. Fixed to be direction-symmetric and to
    quote the real FUNDING_WARN/FUNDING_DANGER thresholds instead of a
    hardcoded claim. No verdict change anywhere here — wording only."""

    def _long(self, funding_rate: float):
        result = _fresh_result()
        result.funding_rate = funding_rate
        return _engine().evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger(),
            result=result,
        )

    def _short(self, funding_rate: float):
        result = _fresh_result()
        result.funding_rate = funding_rate
        h4 = make_candles(H4_DOWNTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_RALLY_INTO_SUPPLY_CLOSES, step_minutes=60)
        return _engine().evaluate(
            h4=h4, h1=h1, m5=m5_short_trigger_deep_sweep(), result=result
        )

    def test_long_danger_tier(self):
        result = self._long(0.0015)
        assert result.verdict == Verdict.APPROVED_LIMIT
        warning = result.funding_warning
        assert warning is not None
        assert "longs" in warning
        assert "elevated squeeze risk" in warning
        assert "zone" not in warning  # that phrasing belongs to the mild tier

    def test_short_danger_tier_is_direction_correct_with_no_false_band(self):
        # The symmetric squeeze case the old code mislabeled: a SHORT paying
        # heavily negative funding used to get the mild "0.05-0.1% zone"
        # text even though 0.15% sits well outside that band.
        result = self._short(-0.0015)
        assert result.verdict == Verdict.APPROVED_LIMIT
        warning = result.funding_warning
        assert warning is not None
        assert "shorts" in warning
        assert "elevated squeeze risk" in warning
        assert "zone" not in warning
        assert "0.05" not in warning  # no false band claim

    def test_mild_tier_quotes_the_real_configured_band(self):
        for result in (self._long(0.0007), self._short(-0.0007)):
            assert result.verdict == Verdict.APPROVED_LIMIT
            warning = result.funding_warning
            assert warning is not None
            assert "0.05" in warning  # FUNDING_WARN
            assert "0.10" in warning  # FUNDING_DANGER
            assert "zone" in warning
            assert "your call" in warning

    def test_no_warning_below_the_threshold(self):
        result = self._long(0.0002)
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.funding_warning is None
