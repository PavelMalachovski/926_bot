"""Owner decision D23 (2026-08-31): show the counter-H4 setup too.

From the ETH case the owner brought: he reads the reversal on 5m/1h and
waits to go long, while Rule 1 — reading H4 — is looking for a short. The
bot was silent on the very trade he was watching, because the H4 direction
is the only one it ever evaluated.

Now, when H4 and H1 trend opposite ways, a SECOND pure pass runs on the
candles the first pass already fetched and the H1-side setup is announced
alongside the H4-side one. It adds, it never replaces: the primary result
is untouched, the counter one is labelled "against H4" and can never earn
the ⭐ (D6 already denies the star whenever the trends disagree).
"""

from datetime import datetime, timezone

import pytest

from app.services.smc.db import Database
from app.services.smc.engine import TripleSyncEngine, trends_disagree
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.notifier import format_result
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import (
    H4_DOWNTREND_CLOSES,
    m5_long_trigger_deep_sweep,
    make_candles,
)

CHECKED_AT = datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc)

# An H1 uptrend (HH + HL) that still carries the untested demand zone
# 3131.0-3138.0 the long M5 fixture triggers from — so H4 reads DOWN, H1
# reads UP, and the disagreement is real rather than staged.
H1_UPTREND_CLOSES = [
    3050, 3070, 3090, 3110, 3130,
    3120, 3105, 3095,
    3115, 3135, 3150,
    3145, 3138, 3132,
    3145, 3160, 3180, 3200, 3220,
    3210, 3195, 3180, 3165,
]

H4 = make_candles(H4_DOWNTREND_CLOSES, step_minutes=240)
H1 = make_candles(H1_UPTREND_CLOSES, step_minutes=60)


def _run(force=None, m5=None):
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=CHECKED_AT
    )
    result.session_name = "New York"
    engine = TripleSyncEngine(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0
    )
    return engine.evaluate(
        h4=H4, h1=H1, m5=m5 or m5_long_trigger_deep_sweep(),
        result=result, force_direction=force,
    )


class TestFixturePremise:
    def test_the_two_timeframes_really_disagree(self):
        primary = _run()
        assert primary.h4_trend == Trend.DOWN
        assert primary.h1_trend == Trend.UP
        assert trends_disagree(primary.h4_trend, primary.h1_trend)

    def test_the_primary_pass_is_looking_the_other_way(self):
        primary = _run()
        assert primary.verdict == Verdict.WATCH
        assert primary.direction_source == "h4"
        assert "Supply" in primary.reasons[0]


class TestForcedDirection:
    def test_it_trades_the_side_it_is_given(self):
        counter = _run(Direction.LONG)
        assert counter.verdict == Verdict.APPROVED_LIMIT
        assert counter.setup.direction == Direction.LONG
        assert counter.direction_source == "h1_counter"

    def test_it_can_never_earn_the_star(self):
        counter = _run(Direction.LONG)
        assert counter.setup.tier_star is False
        assert "trend" in counter.setup.tier_missed

    def test_the_header_says_it_runs_against_h4(self):
        text = format_result(_run(Direction.LONG))
        assert "⚠️ against H4 — direction from the H1 uptrend" in text
        assert "⚠️ counter-hourly" in text

    def test_the_stop_and_entry_are_the_ordinary_rules(self):
        """Forcing the direction bypasses Rule 1 and nothing else."""
        counter = _run(Direction.LONG)
        setup = counter.setup
        assert setup.entry > setup.stop_loss
        assert counter.h1_zone.bottom == 3131.0


def _watcher(tmp_path):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    watcher = Watcher.__new__(Watcher)
    watcher.db = db
    watcher.state = WatcherState(db)
    return watcher


def _primary_with_candles(m5=None):
    result = _run(m5=m5)
    result.m5_candles = m5 or m5_long_trigger_deep_sweep()
    result.h4_candles = H4
    result.h1_candles = H1
    return result


class TestWatcherPass:
    def test_it_finds_the_setup_the_primary_pass_missed(self, tmp_path):
        watcher = _watcher(tmp_path)
        counter = watcher._counter_trend_result("ETHUSD", _primary_with_candles())
        assert counter is not None
        assert counter.setup.direction == Direction.LONG
        assert counter.direction_source == "h1_counter"
        assert counter.setup.tier_star is False

    def test_agreeing_timeframes_never_trigger_a_second_pass(self, tmp_path):
        watcher = _watcher(tmp_path)
        result = _primary_with_candles()
        result.h1_trend = Trend.DOWN  # now both point down
        assert watcher._counter_trend_result("ETHUSD", result) is None

    def test_it_stands_down_when_the_primary_is_already_that_side(self, tmp_path):
        watcher = _watcher(tmp_path)
        result = _primary_with_candles()
        result.setup = _run(Direction.LONG).setup  # primary already long
        assert watcher._counter_trend_result("ETHUSD", result) is None

    def test_a_result_with_no_candles_is_skipped(self, tmp_path):
        watcher = _watcher(tmp_path)
        result = _run()
        assert watcher._counter_trend_result("ETHUSD", result) is None

    def test_off_session_is_skipped(self, tmp_path):
        watcher = _watcher(tmp_path)
        result = _primary_with_candles()
        result.verdict = Verdict.OFF_SESSION
        assert watcher._counter_trend_result("ETHUSD", result) is None

    def test_none_result_is_skipped(self, tmp_path):
        assert _watcher(tmp_path)._counter_trend_result("ETHUSD", None) is None


class TestDedupSlots:
    def test_the_two_tracks_do_not_overwrite_each_other(self, tmp_path):
        """Sharing one slot would re-announce both sides every cycle."""
        watcher = _watcher(tmp_path)
        primary = _run()
        primary.setup = _run(Direction.LONG).setup  # any setup; source is "h4"
        counter = _run(Direction.LONG)

        watcher._dedup_store(primary)["ETHUSD"] = "fp-primary"
        watcher._dedup_store(counter)["ETHUSD"] = "fp-counter"

        assert watcher.state.last_setup["ETHUSD"] == "fp-primary"
        assert watcher.state.last_counter_setup["ETHUSD"] == "fp-counter"

    def test_a_profile_change_clears_both(self, tmp_path):
        watcher = _watcher(tmp_path)
        watcher.state.last_setup["ETHUSD"] = "a"
        watcher.state.last_counter_setup["ETHUSD"] = "b"
        watcher.state.set_profile("ETHUSD", "aggressive")
        assert "ETHUSD" not in watcher.state.last_setup
        assert "ETHUSD" not in watcher.state.last_counter_setup

    def test_the_counter_slot_survives_a_restart(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        state = WatcherState(db)
        state.last_counter_setup["ETHUSD"] = "fp"
        state.save()
        assert WatcherState(db).last_counter_setup == {"ETHUSD": "fp"}
