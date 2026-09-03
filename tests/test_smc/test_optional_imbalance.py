"""Owner decision D22 (2026-08-30): the imbalance stops gating the signal.

"Сила трёх + имбаланс" becomes Triple Sync with the imbalance as a label:
the third sync is the M5 CHoCH on its own. A valid gap still supplies the
best entry and is still required for the ⭐ tier, but its absence costs the
star, not the alert — and the message must still SHOW whatever gap was
found (or say plainly that there was none).

Rule 5 grows an entry ladder for the no-gap case: the M5 order block of the
same touch→CHoCH excursion, else price itself (a market entry on the CHoCH).
Rule 6's stop is untouched — it never read the imbalance.

`SMC_REQUIRE_IMBALANCE=true` restores the pre-D22 gate without a deploy,
the same escape hatch SMC_PD_BASIS gives D17.
"""

from datetime import datetime, timezone

import pytest

from app.services.smc import sniper
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.models import AnalysisResult, Direction, Verdict
from app.services.smc.notifier import format_quiet_setup, format_result
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_no_fvg,
    m5_long_trigger_deep_sweep,
    make_candles,
)

CHECKED_AT = datetime(2026, 7, 6, 15, 40, tzinfo=timezone.utc)


def _run(m5, *, require_imbalance=False, min_fvg=2.0):
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=CHECKED_AT
    )
    result.session_name = "New York"
    result.m5_candles = m5
    engine = TripleSyncEngine(
        min_fvg_size=min_fvg,
        sl_buffer=2.0,
        min_rr=1.0,
        max_entry_gap_r=99.0,
        require_imbalance=require_imbalance,
    )
    return engine.evaluate(
        h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        m5=m5,
        result=result,
    )


class TestSignalWithoutAnImbalance:
    def test_a_choch_with_no_gap_is_now_a_setup(self):
        result = _run(m5_long_no_fvg())
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup is not None
        assert result.setup.fvg is None

    def test_the_entry_falls_to_the_m5_order_block(self):
        setup = _run(m5_long_no_fvg()).setup
        # The last bearish candle of the touch→CHoCH window is index 10
        # (3134 → 3131): demand 3130.0–3134.0, so the limit sits at its top.
        assert setup.entry_source == "ob"
        assert setup.entry == pytest.approx(3134.0)

    def test_the_stop_is_unchanged_by_the_missing_gap(self):
        # Rule 6 reads the swept extreme, never the imbalance: the sweep low
        # is 3130.0 and the buffer is 2.0, exactly as it would be with a gap.
        setup = _run(m5_long_no_fvg()).setup
        assert setup.stop_loss == pytest.approx(3128.0)
        assert setup.entry > setup.stop_loss

    def test_the_missing_gap_costs_the_star_and_nothing_else(self):
        setup = _run(m5_long_no_fvg()).setup
        assert setup.tier_star is False
        assert setup.tier_missed == ["imbalance"]

    def test_no_deeper_order_block_is_advertised_when_it_is_the_entry(self):
        # The block IS the entry here; showing it again as a "deeper entry"
        # would price the same trade twice.
        assert _run(m5_long_no_fvg()).setup.order_block is None

    def test_a_valid_gap_still_wins_the_entry(self):
        setup = _run(m5_long_trigger_deep_sweep()).setup
        assert setup.entry_source == "fvg"
        assert setup.fvg is not None
        assert setup.entry == pytest.approx(setup.fvg.top)
        assert "imbalance" not in setup.tier_missed


class TestLegacyGate:
    def test_require_imbalance_restores_the_old_watch(self):
        result = _run(m5_long_no_fvg(), require_imbalance=True)
        assert result.verdict == Verdict.WATCH
        assert result.setup is None
        assert "no valid FVG" in result.reasons[0]

    def test_the_flag_does_not_touch_a_setup_that_has_a_gap(self):
        with_gate = _run(m5_long_trigger_deep_sweep(), require_imbalance=True)
        without = _run(m5_long_trigger_deep_sweep())
        assert with_gate.verdict == without.verdict == Verdict.APPROVED_LIMIT
        assert with_gate.setup.entry == without.setup.entry
        assert with_gate.setup.stop_loss == without.setup.stop_loss


class TestRejectedGapIsReported:
    def test_a_gap_that_failed_rule_4_is_carried_but_not_entered(self):
        # The deep-sweep fixture's gap is 2.5 wide; demanding 5.0 rejects it
        # on size while leaving it visible.
        result = _run(m5_long_trigger_deep_sweep(), min_fvg=5.0)
        setup = result.setup
        assert setup.fvg is None, "a rejected gap must never become the entry"
        assert setup.rejected_fvg is not None
        assert setup.rejected_fvg_problems == ["size"]
        assert setup.entry != setup.rejected_fvg.top

    def test_the_alert_names_the_flaw(self):
        text = format_result(_run(m5_long_trigger_deep_sweep(), min_fvg=5.0))
        assert "✗ too small" in text
        assert "too small" in text.split("ref · FVG")[1]


class TestMessages:
    def test_the_alert_says_there_was_no_gap(self):
        text = format_result(_run(m5_long_no_fvg()))
        assert "⚡ M5 imbalance         none — the impulse left no gap" in text
        assert "🧱 M5 order block       3134.00   ← limit order (no imbalance)" in text
        assert "ref · no M5 imbalance in the impulse" in text

    def test_the_ladder_header_names_the_rung_the_rr_is_measured_from(self):
        assert "RR from OB" in format_result(_run(m5_long_no_fvg()))
        assert "RR from FVG" in format_result(_run(m5_long_trigger_deep_sweep()))

    def test_the_quiet_line_reports_the_missing_imbalance(self):
        line = format_quiet_setup(_run(m5_long_no_fvg()))
        assert "Missed for ⭐: imbalance" in line

    def test_the_chart_survives_a_setup_with_no_gap(self):
        from app.services.smc import chart

        png = chart.render_setup_chart(_run(m5_long_no_fvg()))
        assert png and len(png) > 1000


class TestClassify:
    def test_the_imbalance_is_a_star_condition(self):
        verdict = sniper.classify(
            room=2.0, sweep="PDL", pd="ok", stale=False, has_imbalance=False
        )
        assert verdict.star is False
        assert verdict.missed == ["imbalance"]

    def test_a_caller_that_does_not_report_one_keeps_the_star(self):
        # Default True: "not the thing being judged here", not a silent loss.
        verdict = sniper.classify(room=2.0, sweep="PDL", pd="ok", stale=False)
        assert verdict.star is True


class TestGeometryGuard:
    def test_an_entry_on_the_wrong_side_of_the_stop_is_skipped(self, monkeypatch):
        """A defensive branch, tested at its own level.

        The FVG entry sat on the trading side of its stop by construction —
        the gap is part of the impulse away from the swept extreme. The D22
        rungs are new geometry (the order block always clears it too; the
        market rung does not have to, if price collapses back through the
        swept low right after the CHoCH), so the inverted case is checked
        rather than assumed. Driving it through candles alone would need
        price to close below the stop while still holding the zone the stop
        sits under — a contortion that would test the fixture, not the rule.
        Forcing the stop reference above the entry states the rule directly:
        a LONG whose stop sits above its entry is malformed, not tradeable.
        """
        from app.services.smc import engine as engine_module

        monkeypatch.setattr(
            engine_module, "sweep_extreme", lambda *a, **kw: 3200.0
        )
        result = _run(m5_long_no_fvg())
        assert result.verdict == Verdict.SKIP
        assert "wrong side of the SL" in result.reasons[0]
        assert result.setup is None

    def test_the_reachable_rungs_all_clear_the_stop(self):
        """The invariant the guard exists to protect, on real fixtures."""
        for m5 in (m5_long_no_fvg(), m5_long_trigger_deep_sweep()):
            setup = _run(m5).setup
            assert setup.direction == Direction.LONG
            assert setup.entry > setup.stop_loss


class TestStaleEntryWording:
    def test_a_stale_order_block_entry_names_the_block(self):
        """Rule 5.1's warning said "past the imbalance" whatever rung the
        entry came from; on the D22 order-block rung there is no imbalance
        to have run past (audit 2026-09-03). The "price has run" prefix is
        the backtest autopsy's key and stays."""
        result = AnalysisResult(
            symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=CHECKED_AT
        )
        result.session_name = "New York"
        # entry 3134.0 (the block's top), stop 3128.0: risk 6.0, so a price of
        # 3150.0 is 16.0 / 6.0 = 2.7R past the entry — stale at 0.5R.
        result.price = 3150.0
        engine = TripleSyncEngine(
            min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=0.5,
        )
        result = engine.evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_no_fvg(),
            result=result,
        )
        assert result.setup is not None, result.reasons
        assert result.setup.entry_source == "ob"
        assert "stale" in result.setup.tier_missed
        assert "price has run 2.7R past the order block" in result.warnings
        assert not any("imbalance" in w for w in result.warnings)

    def test_a_stale_imbalance_entry_still_says_imbalance(self):
        result = AnalysisResult(
            symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=CHECKED_AT
        )
        result.session_name = "New York"
        result.price = 3160.0
        engine = TripleSyncEngine(
            min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=0.5,
        )
        result = engine.evaluate(
            h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
            h1=make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
            m5=m5_long_trigger_deep_sweep(),
            result=result,
        )
        assert result.setup is not None, result.reasons
        assert result.setup.entry_source == "fvg"
        assert any(w.endswith("past the imbalance") for w in result.warnings)
