"""The stored plan and the cheap monitor: fills, invalidation, the watch
zone, expiry, and trades tracked to TP1/SL."""

from datetime import timedelta

from app.services.opus.decision import parse_decision
from app.services.opus.plan import (
    ENTERED, EV_EXPIRED, EV_FILLED, EV_INVALIDATED, EV_ZONE_REACHED, EXPIRED,
    FILLED, IDLE, OPEN, PENDING, SL, TP, TIMEOUT, WATCHING, advance_plan,
    advance_trade, plan_from_decision, trade_from_plan,
)
from app.services.smc.models import Candle
from tests.test_opus.helpers import GOOD_LIMIT, GOOD_WAIT, NOW


def candle(minutes_after: int, o, h, l, c) -> Candle:  # noqa: E741
    return Candle(timestamp=NOW + timedelta(minutes=minutes_after), open=o, high=h, low=l, close=c)


def limit_plan(**overrides):
    d = parse_decision({**GOOD_LIMIT, **overrides})
    return plan_from_decision("USDJPY", d, NOW, 147.25, NOW + timedelta(hours=3), "09.09 10:30")


def wait_plan(**overrides):
    d = parse_decision({**GOOD_WAIT, **overrides})
    return plan_from_decision("USDJPY", d, NOW, 147.25, NOW + timedelta(hours=8), "09.09 10:30")


class TestPlanFromDecision:
    def test_statuses(self):
        assert limit_plan().status == PENDING
        assert wait_plan().status == WATCHING
        assert wait_plan(watch_low=None, watch_high=None).status == IDLE
        assert limit_plan(action="market").status == ENTERED

    def test_roundtrip(self):
        from app.services.opus.plan import OpusPlan

        p = limit_plan()
        assert OpusPlan.from_dict(p.to_dict()) == p


class TestLimitMonitor:
    def test_fill_on_touch(self):
        p = limit_plan()  # short 148.10
        events = advance_plan(p, [candle(5, 147.9, 148.0, 147.8, 147.95),
                                  candle(10, 147.95, 148.12, 147.9, 148.0)], NOW + timedelta(minutes=15))
        assert [e.kind for e in events] == [EV_FILLED]
        assert p.status == FILLED and p.resolved_at

    def test_candles_before_the_plan_are_ignored(self):
        p = limit_plan()
        old = candle(-30, 148.0, 148.5, 147.9, 148.2)  # touched the entry long before
        assert advance_plan(p, [old, candle(5, 147.9, 148.0, 147.8, 147.95)], NOW) == []
        assert p.status == PENDING

    def test_in_progress_candle_of_the_press_counts(self):
        p = limit_plan()  # created 08:30; the 08:30 candle is in progress
        assert [e.kind for e in advance_plan(p, [candle(0, 147.9, 148.2, 147.8, 148.0)], NOW)] == [EV_FILLED]

    def test_limit_invalidation_is_the_move_leaving(self):
        p = limit_plan()  # short 148.10, invalidation 146.90 below the market
        wick = candle(5, 147.2, 147.3, 146.8, 147.0)  # wick below, close back above
        assert advance_plan(p, [wick], NOW) == []
        drop = candle(10, 147.0, 147.05, 146.7, 146.8)  # body close below 146.90
        events = advance_plan(p, [wick, drop], NOW)
        assert [e.kind for e in events] == [EV_INVALIDATED]
        assert p.status == PENDING  # the plan stays until Opus or the owner decides
        # fires once
        assert advance_plan(p, [wick, drop, candle(15, 146.8, 146.85, 146.6, 146.7)], NOW) == []

    def test_fill_wins_over_invalidation_in_one_candle(self):
        p = limit_plan()
        both = candle(5, 147.2, 148.2, 146.7, 146.8)
        assert [e.kind for e in advance_plan(p, [both], NOW)] == [EV_FILLED]

    def test_expiry(self):
        p = limit_plan()
        events = advance_plan(p, [candle(5, 147.9, 148.0, 147.8, 147.95)], NOW + timedelta(hours=4))
        assert [e.kind for e in events] == [EV_EXPIRED] and p.status == EXPIRED

    def test_expiry_without_candles(self):
        p = limit_plan()
        assert [e.kind for e in advance_plan(p, [], NOW + timedelta(hours=4))] == [EV_EXPIRED]

    def test_resolved_plan_is_inert(self):
        p = limit_plan()
        p.status = FILLED
        assert advance_plan(p, [candle(5, 147.9, 148.0, 147.8, 147.95)], NOW + timedelta(hours=9)) == []


class TestWatchMonitor:
    def test_zone_reached_once(self):
        p = wait_plan()  # zone 146.90-147.05
        inside = candle(5, 147.2, 147.25, 147.0, 147.1)
        events = advance_plan(p, [inside], NOW)
        assert [e.kind for e in events] == [EV_ZONE_REACHED]
        assert p.status == WATCHING
        assert advance_plan(p, [inside, candle(10, 147.1, 147.15, 146.95, 147.0)], NOW) == []

    def test_wait_invalidation_follows_the_bias(self):
        p = wait_plan()  # bias long, invalidation 146.80 (below)
        events = advance_plan(p, [candle(5, 147.0, 147.05, 146.6, 146.7)], NOW)
        assert {e.kind for e in events} == {EV_ZONE_REACHED, EV_INVALIDATED}

    def test_neutral_bias_has_no_invalidation_side(self):
        p = wait_plan(bias="neutral")
        assert advance_plan(p, [candle(5, 147.0, 147.05, 146.6, 146.7)], NOW) == [
            e for e in advance_plan(wait_plan(bias="neutral"), [candle(5, 147.0, 147.05, 146.6, 146.7)], NOW)
        ]
        kinds = {e.kind for e in advance_plan(wait_plan(bias="neutral"), [candle(5, 147.0, 147.05, 146.6, 146.7)], NOW)}
        assert kinds == {EV_ZONE_REACHED}


class TestTrade:
    def test_short_to_tp1(self):
        t = trade_from_plan(limit_plan(), NOW, "limit")  # short 148.10 sl 148.40 tp1 147.40
        assert t.status == OPEN and abs(t.rr1 - 2.333) < 0.01
        assert advance_trade(t, [candle(5, 148.0, 148.1, 147.3, 147.5)], NOW) == TP
        assert t.result_r == 2.33 and t.closed_at

    def test_same_candle_is_a_stop(self):
        t = trade_from_plan(limit_plan(), NOW, "limit")
        assert advance_trade(t, [candle(5, 148.0, 148.5, 147.3, 147.5)], NOW) == SL
        assert t.result_r == -1.0

    def test_timeout(self):
        t = trade_from_plan(limit_plan(), NOW, "limit")
        assert advance_trade(t, [candle(5, 148.0, 148.2, 147.9, 148.0)], NOW + timedelta(days=6)) == TIMEOUT

    def test_closed_trade_is_inert(self):
        t = trade_from_plan(limit_plan(), NOW, "limit")
        t.status = TP
        assert advance_trade(t, [candle(5, 148.0, 148.5, 147.3, 147.5)], NOW) is None
