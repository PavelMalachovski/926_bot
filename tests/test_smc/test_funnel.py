from datetime import timedelta

from scripts.funnel import (
    build_factor_profile,
    classify_result,
    parse_factors,
    replay_funnel,
    session_day_span,
)
import scripts.funnel as funnel_mod
from app.services.smc.models import AnalysisResult, Verdict, Trend
from app.services.smc.profiles import get_profile
from app.services.smc.instruments import get_instrument
from tests.test_smc.helpers import (
    SESSION_BASE, H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, candle,
    m5_long_trigger, make_candles,
)


def test_classify_approved():
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT,
                       checked_at=None)
    assert classify_result(r) == "approved"


def test_classify_flat_h4():
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=None)
    r.h4_trend = Trend.FLAT
    r.reasons = ["H4 is flat or CHoCH against the trend — no direction"]
    assert classify_result(r) == "h4_flat"


def test_classify_result_labels_low_rr():
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=SESSION_BASE,
    )
    result.reasons.append(
        "RR 1:0.6 < minimum 1:1 to the nearest liquidity (H1 swing high 3221.00)"
    )
    assert classify_result(result) == "rr_low"


def test_classify_result_labels_reward_inside_sl_buffer_as_no_liquidity():
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=SESSION_BASE,
    )
    result.reasons.append(
        "Nearest liquidity sits inside the SL buffer — no positive "
        "reward to aim at"
    )
    assert classify_result(result) == "no_liquidity"


def test_classify_result_labels_a_stale_entry():
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=SESSION_BASE,
    )
    result.reasons.append(
        "Price has run 1.2R past the entry 3140.50 (max 0.5R) — the limit "
        "would sit too far from market"
    )
    assert classify_result(result) == "entry_stale"


def test_replay_counts_at_least_one_approved_on_the_trigger_fixture():
    # H4/H1 are shifted so their history is fully closed at/before
    # SESSION_BASE (the m5 fixture's start) — a realistic point-in-time
    # snapshot, rather than the old same-origin fixture whose H4/H1 extended
    # chronologically *past* the m5 replay window and got excluded entirely
    # once replay_funnel started point-in-time slicing h4/h1 by timestamp.
    # ... - shifted by one extra step so the LAST bar has fully closed by
    # SESSION_BASE (replay_funnel slices by close time, not open time).
    h4 = make_candles(
        H4_UPTREND_CLOSES,
        start=SESSION_BASE - timedelta(minutes=240 * len(H4_UPTREND_CLOSES)),
        step_minutes=240,
    )
    h1 = make_candles(
        H1_PULLBACK_CLOSES,
        start=SESSION_BASE - timedelta(minutes=60 * len(H1_PULLBACK_CLOSES)),
        step_minutes=60,
    )
    counts = replay_funnel(
        get_instrument("ETHUSD"),
        h4,
        h1,
        m5_long_trigger(),
        profile=get_profile("conservative"),
        # m5_long_trigger's live price sits further than the shipped 0.5R
        # from its FVG entry (see task-7-report.md); this test is about
        # point-in-time h4/h1 slicing, not Rule 5.1, so disable the gate.
        max_entry_gap_r=99.0,
    )
    assert counts["approved"] >= 1


def test_replay_uses_point_in_time_h4h1():
    """The final up-leg of H4_UPTREND_CLOSES (indices 19-24) is timestamped
    *after* the entire m5 replay window closes — i.e. it has not printed yet
    at any replay step. Point-in-time slicing must not let the engine see it:
    only 1 confirmed low exists in the visible (past) H4 candles, so the
    trend reads FLAT and (with the conservative profile, which does not take
    an H4-CHoCH entry) nothing is ever approved. Without the point-in-time
    fix, the full 25-candle H4 array (which the *existing* test above proves
    resolves to a clean uptrend) would be fed at every step and this fixture
    would approve — so an "approved" count of 0 here demonstrates no future
    H4 leaked into the replay."""
    past_closes = H4_UPTREND_CLOSES[:19]
    future_closes = H4_UPTREND_CLOSES[19:]
    h4 = make_candles(
        past_closes,
        start=SESSION_BASE - timedelta(minutes=240 * len(past_closes)),
        step_minutes=240,
    ) + make_candles(
        future_closes,
        start=SESSION_BASE + timedelta(minutes=200),
        step_minutes=240,
    )
    h1 = make_candles(
        H1_PULLBACK_CLOSES,
        start=SESSION_BASE - timedelta(minutes=60 * len(H1_PULLBACK_CLOSES)),
        step_minutes=60,
    )
    m5 = m5_long_trigger()

    counts = replay_funnel(
        get_instrument("ETHUSD"), h4, h1, m5,
        profile=get_profile("conservative"),
    )

    assert counts.get("approved", 0) == 0
    assert counts.get("h4_flat", 0) >= 1
    assert counts["session_days"] >= 1


def test_distinct_setups_collapses_persisting_approved(monkeypatch):
    # classify_result is patched to always report "approved" so a run of
    # consecutive M5 closes simulates a setup that persists across several
    # candles (as a real approved zone/FVG does) without needing an engine
    # fixture that stays approved for many bars.
    monkeypatch.setattr(funnel_mod, "classify_result", lambda result: "approved")

    n_h4h1 = 20
    h4 = make_candles(
        [3000.0] * n_h4h1,
        start=SESSION_BASE - timedelta(minutes=240 * (n_h4h1 - 1)),
        step_minutes=240,
    )
    h1 = make_candles(
        [3000.0] * n_h4h1,
        start=SESSION_BASE - timedelta(minutes=60 * (n_h4h1 - 1)),
        step_minutes=60,
    )
    m5 = make_candles([3000.0] * 7, step_minutes=5)

    counts = funnel_mod.replay_funnel(
        get_instrument("ETHUSD"), h4, h1, m5,
        profile=get_profile("conservative"), warmup=2,
    )

    assert counts["approved"] >= 3
    assert counts["distinct_setups"] == 1


def test_in_progress_h4_bar_is_excluded(monkeypatch):
    """Regression: a bar that OPENED before the replay step but had not yet
    CLOSED must be invisible to the engine (its OHLC contains future prices).
    The old open-time filter leaked it into every point-in-time slice."""
    seen_h4_opens = []

    def spy(self, h4, h1, m5, result):
        seen_h4_opens.append(max(c.timestamp for c in h4))
        result.verdict = Verdict.SKIP
        return result

    monkeypatch.setattr(funnel_mod.TripleSyncEngine, "evaluate", spy)

    n = 20
    h4 = make_candles(
        [3000.0] * n,
        start=SESSION_BASE - timedelta(minutes=240 * n),
        step_minutes=240,
    )
    in_progress = candle(
        3000.0, 3500.0, 2900.0, 3400.0, index=0,
        start=SESSION_BASE - timedelta(minutes=5),
    )
    h4.append(in_progress)
    h1 = make_candles(
        [3000.0] * n,
        start=SESSION_BASE - timedelta(minutes=60 * n),
        step_minutes=60,
    )
    m5 = make_candles([3000.0] * 7, step_minutes=5)

    funnel_mod.replay_funnel(
        get_instrument("ETHUSD"), h4, h1, m5,
        profile=get_profile("conservative"), warmup=2,
    )

    assert seen_h4_opens  # the engine did run
    assert all(ts < in_progress.timestamp for ts in seen_h4_opens)


def test_session_day_span_counts_distinct_prague_days():
    same_day = make_candles([3000.0, 3010.0], start=SESSION_BASE, step_minutes=5)
    assert session_day_span(same_day) == 1

    two_days = [
        candle(3000.0, 3001.0, 2999.0, 3000.0, index=0, start=SESSION_BASE),
        candle(
            3000.0, 3001.0, 2999.0, 3000.0, index=0,
            start=SESSION_BASE + timedelta(days=1),
        ),
    ]
    assert session_day_span(two_days) == 2


def test_factor_sweep_runs_each_factor():
    profile = build_factor_profile(0.6)
    assert profile.fvg_size_factor == 0.6

    assert parse_factors("0.4,0.6") == [0.4, 0.6]
