"""Tests for the in-memory PlanBook (auto-plan feature)."""

from app.services.smc.models import Direction, Trend
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.planbook import PlanBook, PlanEntry, plan_fingerprint


def _scenario(direction=Direction.LONG, bottom=3131.0, top=3138.0,
              rr=2.0, speculative=False):
    entry = top if direction == Direction.LONG else bottom
    return PlanScenario(
        direction=direction, entry=entry, stop_loss=bottom - 2.0,
        take_profit=top + 20.0, rr=rr, zone_bottom=bottom, zone_top=top,
        speculative=speculative,
    )


def _plan(scenarios=None, blocker=None):
    plan = PairPlan(pair="ETHUSD", price=3160.0, price_decimals=2,
                    h4_trend=Trend.UP, scenarios=scenarios or [])
    plan.blocker = blocker
    return plan


def _entry(plan):
    return PlanEntry(plan=plan, data={"h4": [], "h1": [], "m5": []}, as_of="07:55")


class TestFingerprint:
    def test_rr_drift_is_not_material(self):
        a = _plan([_scenario(rr=2.0)])
        b = _plan([_scenario(rr=1.4)])
        assert plan_fingerprint(a) == plan_fingerprint(b)

    def test_zone_shift_is_material(self):
        a = _plan([_scenario(bottom=3131.0, top=3138.0)])
        b = _plan([_scenario(bottom=3135.0, top=3142.0)])
        assert plan_fingerprint(a) != plan_fingerprint(b)

    def test_scenario_appearing_is_material(self):
        assert plan_fingerprint(_plan([])) != plan_fingerprint(_plan([_scenario()]))

    def test_blocker_change_is_material(self):
        a = _plan(blocker="Wait for a fresh H1 zone to form (an untested HL)")
        b = _plan(blocker="Price is below the H1 Demand zone 3131.00-3138.00")
        assert plan_fingerprint(a) != plan_fingerprint(b)

    def test_speculative_flag_is_material(self):
        a = _plan([_scenario(speculative=False)])
        b = _plan([_scenario(speculative=True)])
        assert plan_fingerprint(a) != plan_fingerprint(b)


class TestPlanBook:
    def test_get_is_case_insensitive(self):
        book = PlanBook()
        book.update("ethusd", _entry(_plan()))
        assert book.get("ETHUSD") is not None

    def test_scenario_for_touch_matches_overlap(self):
        book = PlanBook()
        book.update("ETHUSD", _entry(_plan([_scenario(bottom=3131.0, top=3138.0)])))
        # candle wick dips to 3137.5 — overlaps the zone top
        assert book.scenario_for_touch("ETHUSD", 3137.5, 3145.0) is not None
        # candle fully above the zone — no touch
        assert book.scenario_for_touch("ETHUSD", 3139.0, 3145.0) is None
        # unknown pair
        assert book.scenario_for_touch("EURUSD", 0.0, 9999.0) is None

    def test_has_zone_filters_by_direction(self):
        book = PlanBook()
        book.update("ETHUSD", _entry(_plan([_scenario(Direction.LONG)])))
        assert book.has_zone("ETHUSD", 3130.0, 3139.0, "long")
        assert not book.has_zone("ETHUSD", 3130.0, 3139.0, "short")
        assert book.has_zone("ETHUSD", 3130.0, 3139.0, None)
