"""The decision parser and the hard limits (owner picks 2026-09-13:
session, blackout, minimum RR) plus order geometry."""

import json

from app.services.opus.decision import (
    Decision, Limits, downgrade, parse_decision, violations,
)
from tests.test_opus.helpers import GOOD_LIMIT, GOOD_WAIT


def limits(**kw) -> Limits:
    base = dict(price=147.25, min_rr=2.0, session_open=True, blackout_until=None, decimals=3)
    base.update(kw)
    return Limits(**base)


class TestParse:
    def test_parses_a_limit(self):
        d = parse_decision(json.dumps(GOOD_LIMIT), model="m")
        assert d.action == "limit" and d.direction == "short"
        assert d.entry == 148.10 and d.tp2 == 147.00
        assert abs(d.rr(d.tp1) - 2.333) < 0.01
        assert d.model == "m"

    def test_parses_a_wait_with_zone(self):
        d = parse_decision(GOOD_WAIT)
        assert d.action == "wait" and d.has_watch_zone and d.direction == "none"

    def test_garbage_is_none(self):
        assert parse_decision("not json") is None
        assert parse_decision({"action": "limit"}) is None  # no read
        assert parse_decision({"action": "sell", "read": "x"}) is None

    def test_confidence_is_clamped(self):
        d = parse_decision({**GOOD_WAIT, "confidence": 9})
        assert d.confidence == 5
        d = parse_decision({**GOOD_WAIT, "confidence": "abc"})
        assert d.confidence == 3

    def test_strings_are_bounded(self):
        d = parse_decision({**GOOD_WAIT, "risks": ["a"] * 10, "reasons": [""]})
        assert len(d.risks) == 4 and d.reasons == []


class TestViolations:
    def test_good_limit_passes(self):
        assert violations(parse_decision(GOOD_LIMIT), limits()) == []

    def test_good_wait_passes(self):
        assert violations(parse_decision(GOOD_WAIT), limits()) == []

    def test_rr_below_minimum(self):
        d = parse_decision({**GOOD_LIMIT, "tp1": 147.70})  # 0.40 reward / 0.30 risk
        broken = violations(d, limits())
        assert len(broken) == 1 and "RR to tp1 is 1:1.3" in broken[0]

    def test_geometry_short(self):
        d = parse_decision({**GOOD_LIMIT, "stop_loss": 147.90})  # stop below a short entry
        assert any("short geometry" in v for v in violations(d, limits()))

    def test_sell_limit_below_market_is_rejected(self):
        d = parse_decision({**GOOD_LIMIT, "entry": 147.10, "stop_loss": 147.40, "tp1": 146.40})
        broken = violations(d, limits(price=147.25))
        assert any("ABOVE the market" in v for v in broken)

    def test_buy_limit_above_market_is_rejected(self):
        d = parse_decision({
            **GOOD_LIMIT, "direction": "long", "entry": 147.50, "stop_loss": 147.20,
            "tp1": 148.20, "tp2": None, "invalidation": 147.10,
        })
        assert any("BELOW the market" in v for v in violations(d, limits(price=147.25)))

    def test_market_entry_is_the_price(self):
        d = parse_decision({**GOOD_LIMIT, "action": "market", "entry": 999.0,
                            "stop_loss": 147.55, "tp1": 146.60, "tp2": None,
                            "invalidation": 147.60})
        assert violations(d, limits(price=147.25)) == []
        assert d.entry == 147.25

    def test_market_off_session_is_rejected(self):
        d = parse_decision({**GOOD_LIMIT, "action": "market", "stop_loss": 147.55,
                            "tp1": 146.60, "tp2": None, "invalidation": 147.60})
        broken = violations(d, limits(session_open=False))
        assert broken and "no market entry" in broken[0]

    def test_limit_off_session_is_only_a_note(self):
        d = parse_decision(GOOD_LIMIT)
        broken = violations(d, limits(session_open=False))
        assert broken and "planned" in broken[0]

    def test_blackout_blocks_every_order(self):
        for action in ("limit", "market"):
            d = parse_decision({**GOOD_LIMIT, "action": action})
            broken = violations(d, limits(blackout_until="14:45"))
            assert any("blackout until 14:45" in v for v in broken), action

    def test_limit_invalidation_on_the_stop_side_is_rejected(self):
        d = parse_decision({**GOOD_LIMIT, "invalidation": 148.45})  # above a short's market
        assert any("target side" in v for v in violations(d, limits()))

    def test_market_drops_the_invalidation(self):
        d = parse_decision({**GOOD_LIMIT, "action": "market", "stop_loss": 147.55,
                            "tp1": 146.60, "tp2": None, "invalidation": 146.0})
        assert violations(d, limits()) == [] and d.invalidation is None

    def test_wait_invalidation_must_be_beyond_the_zone(self):
        d = parse_decision({**GOOD_WAIT, "invalidation": 147.00})  # inside the long zone
        assert any("BELOW the watch zone" in v for v in violations(d, limits()))
        d = parse_decision({**GOOD_WAIT, "bias": "short", "invalidation": 146.80})
        assert any("ABOVE the watch zone" in v for v in violations(d, limits()))

    def test_tp2_must_extend_tp1(self):
        d = parse_decision({**GOOD_LIMIT, "tp2": 147.60})
        assert any("tp2" in v for v in violations(d, limits()))

    def test_watch_zone_needs_both_edges(self):
        d = parse_decision({**GOOD_WAIT, "watch_high": None})
        assert violations(d, limits()) == ["a watch zone needs both watch_low and watch_high"]
        d = parse_decision({**GOOD_WAIT, "watch_low": 147.10})
        assert violations(d, limits()) == ["watch_low must be below watch_high"]

    def test_order_without_numbers(self):
        d = parse_decision({**GOOD_LIMIT, "stop_loss": None})
        assert any("needs entry, stop_loss and tp1" in v for v in violations(d, limits()))


class TestDowngrade:
    def test_order_becomes_wait_and_keeps_numbers(self):
        d = downgrade(parse_decision(GOOD_LIMIT), ["RR too thin"])
        assert d.action == "wait" and d.downgraded_from == "limit"
        assert d.entry == 148.10 and d.notes == ["RR too thin"]

    def test_wait_stays_wait(self):
        d = downgrade(parse_decision(GOOD_WAIT), ["zone broken"])
        assert d.action == "wait" and d.downgraded_from is None
