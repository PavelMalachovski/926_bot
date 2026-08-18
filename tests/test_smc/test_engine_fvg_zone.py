"""The engine end-to-end with an H1 IMBALANCE as the zone of interest.

`find_h1_zone` genuinely returns None on this H1 fixture, so Rule 2 really
falls through to `find_h1_fvg_zone` (D4, spec §2.1) — no monkeypatching of
either finder. What the fixture then proves is the whole point of the
fallback path: an H1 gap is a band the bot WAITS at.

Geometry (all prices are ETHUSD-scale, `_engine` uses min_fvg 2.0 /
sl_buffer 2.0):

* H1 rallies 3100 -> 3122 in six hourly candles. It has no confirmed low
  pivot at all (a monotone rise cannot make one), so there is no order
  block; the impulse candle at 17:00 leaves an untouched gap 3103.00 -
  3119.60 whose formation candle is the 18:00 one.
* M5 covers 17:00-19:55 (phase A) — that is, the M5 window contains the
  gap's own birth. The 17:00-17:45 candles traverse the band on the way
  up (that is what an impulse does), and the 18:00 candle's low IS the
  band's edge. Neither is a pullback into the zone: price has been above
  the band ever since it formed.
* Phase B appends the 20:00-20:55 hour — the pullback that genuinely
  re-enters the band (20:10-20:30), sweeps to 3113.00 and reverses with a
  CHoCH at 20:35 and an impulse FVG.

The H1 array stops at the 19:00 candle on purpose: engines see closed
candles only, so the pullback happens inside an hour whose H1 candle has
not printed yet — which is exactly why the H1 gap still reads untouched
while M5 already shows the excursion.
"""

from datetime import timedelta

from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Direction, Verdict
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.structure import (
    find_h1_zone,
    find_zone_of_interest,
    sweep_extreme,
    zone_touch_span,
)
from tests.test_smc.helpers import H4_UPTREND_CLOSES, SESSION_BASE, make_candles

# One hourly candle per element; the 17:00 impulse opens the gap.
_H1_GAP_UP = [3100, 3101, 3102, 3120, 3121, 3122]

_ZONE_BOTTOM, _ZONE_TOP = 3103.0, 3119.6

# M5, five-minute closes, one list per hour of the fixture.
_M5_1700 = [3104, 3106, 3108, 3110, 3112, 3114, 3116, 3118, 3120, 3122, 3124, 3123]
_M5_1800 = [3124, 3126, 3125, 3123, 3122, 3124, 3126, 3125, 3123, 3122, 3123, 3125]
_M5_1900 = [3124, 3122, 3123, 3125, 3126, 3124, 3122, 3123, 3124, 3125, 3123, 3122]
# 20:00 — the pullback: down into the band, sweep at 3113.00, CHoCH back up.
_M5_2000 = [3128, 3124, 3120, 3116, 3114, 3118, 3124, 3130, 3132, 3131, 3132, 3133]

# Index of the first M5 candle that exists after the gap's formation candle
# opened (18:00) — nothing before it can belong to an excursion into a band
# that was still being drawn.
_FIRST_POST_FORMATION = len(_M5_1700)


def _h1():
    return make_candles(_H1_GAP_UP, start=SESSION_BASE, step_minutes=60)


def _m5(*hours):
    closes = [c for hour in hours for c in hour]
    return make_candles(
        closes, start=SESSION_BASE + timedelta(hours=3), step_minutes=5
    )


def _result(m5) -> AnalysisResult:
    return AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=m5[-1].timestamp
    )


def _run(m5) -> AnalysisResult:
    engine = TripleSyncEngine(
        instrument=get_instrument("ETHUSD"),
        min_fvg_size=2.0,
        sl_buffer=2.0,
        min_rr=1.0,
        max_entry_gap_r=99.0,
    )
    return engine.evaluate(
        h4=make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        h1=_h1(),
        m5=m5,
        result=_result(m5),
    )


class TestEngineWithAnH1ImbalanceZone:
    def test_the_fallback_really_is_the_imbalance(self):
        """No mocks: the order block is absent, the gap is the zone."""
        h1 = _h1()
        assert find_h1_zone(h1, Direction.LONG, max_touches=0) is None
        zone = find_zone_of_interest(
            h1, Direction.LONG, min_size=2.0, max_touches=0
        )
        assert zone is not None
        assert zone.kind == "FVG"
        assert (zone.bottom, zone.top) == (_ZONE_BOTTOM, _ZONE_TOP)

    def test_a_freshly_formed_gap_is_still_a_pullback_wait(self):
        """The zone was drawn by the impulse and price never came back.

        The candles that made the gap are not an excursion into it, so the
        verdict is Rule 3's "not reached yet" — the state the whole H1-FVG
        feature exists to produce, and the one the zone alert fires from.
        """
        result = _run(_m5(_M5_1700, _M5_1800, _M5_1900))
        assert result.verdict == Verdict.WATCH
        assert result.h1_zone is not None and result.h1_zone.kind == "FVG"
        assert result.reasons
        assert "has not reached the H1 Demand zone" in result.reasons[0]
        assert result.setup is None
        assert not result.in_zone

    def test_no_excursion_before_the_gap_existed(self):
        """The same claim at the structure level: no span at all."""
        zone = find_zone_of_interest(
            _h1(), Direction.LONG, min_size=2.0, max_touches=0
        )
        assert zone_touch_span(_m5(_M5_1700, _M5_1800, _M5_1900), zone) is None

    def test_a_genuine_pullback_anchors_inside_the_excursion(self):
        """Price returns: the touch anchor and the Rule 6 stop reference
        both come from the pullback, not from the gap's own birth."""
        m5 = _m5(_M5_1700, _M5_1800, _M5_1900, _M5_2000)
        zone = find_zone_of_interest(
            _h1(), Direction.LONG, min_size=2.0, max_touches=0
        )
        span = zone_touch_span(m5, zone)
        assert span is not None
        touch, end = span
        assert touch >= _FIRST_POST_FORMATION
        assert m5[touch].timestamp >= zone.timestamp
        # The sweep is the pullback's own low (3113.00), not the lowest
        # price in the pre-formation traversal (3103.60).
        assert sweep_extreme(m5, Direction.LONG, touch, end + 1) == 3113.0

        result = _run(m5)
        assert result.verdict == Verdict.APPROVED_LIMIT
        assert result.setup is not None
        # 3113.00 sweep - 2.00 buffer. An anchor before the zone formed
        # would drag this down to 3103.60 - 2.00.
        assert result.setup.stop_loss == 3111.0
