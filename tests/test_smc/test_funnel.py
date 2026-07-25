from scripts.funnel import replay_funnel, classify_result
from app.services.smc.models import AnalysisResult, Verdict, Trend
from app.services.smc.profiles import get_profile
from app.services.smc.instruments import get_instrument
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, m5_long_trigger, make_candles,
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


def test_replay_counts_at_least_one_approved_on_the_trigger_fixture():
    counts = replay_funnel(
        get_instrument("ETHUSD"),
        make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5_long_trigger(),
        profile=get_profile("conservative"),
        min_rr=2.0,
    )
    assert counts["approved"] >= 1
