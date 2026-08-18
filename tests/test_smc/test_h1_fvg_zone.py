"""An untouched H1 imbalance as a zone of interest (spec §2.1, D10)."""

from app.services.smc.models import Direction
from app.services.smc.structure import find_h1_fvg_zone
from tests.test_smc.helpers import make_candles


def _gap_up():
    """Closes that leave a bullish H1 gap and never trade back into it.

    make_candles builds each candle around its close, so a jump between
    consecutive closes opens a gap between candle i-2's high and candle
    i's low. Read helpers.py before adjusting these numbers.
    """
    return make_candles(
        [100.0, 101.0, 102.0, 120.0, 121.0, 122.0, 123.0, 124.0],
        step_minutes=60,
    )


class TestFindH1FvgZone:
    def test_finds_an_untouched_bullish_gap(self):
        zone = find_h1_fvg_zone(_gap_up(), Direction.LONG, min_size=1.0)
        assert zone is not None
        assert zone.is_demand is True
        assert zone.kind == "FVG"
        assert zone.bottom < zone.top

    def test_gap_smaller_than_min_size_is_rejected(self):
        zone = find_h1_fvg_zone(_gap_up(), Direction.LONG, min_size=500.0)
        assert zone is None

    def test_no_gap_no_zone(self):
        flat = make_candles([100.0, 100.5, 101.0, 101.5, 102.0], step_minutes=60)
        assert find_h1_fvg_zone(flat, Direction.LONG, min_size=0.01) is None

    def test_wrong_direction_finds_nothing(self):
        assert find_h1_fvg_zone(_gap_up(), Direction.SHORT, min_size=1.0) is None

    def test_a_touched_gap_is_not_fresh(self):
        """D10: any penetration disqualifies it — price already arrived."""
        candles = make_candles(
            [100.0, 101.0, 102.0, 120.0, 121.0, 110.0, 121.0, 122.0],
            step_minutes=60,
        )
        assert find_h1_fvg_zone(candles, Direction.LONG, min_size=1.0) is None

    def test_the_freshest_gap_wins_when_several_are_untouched(self):
        candles = make_candles(
            [100.0, 101.0, 102.0, 120.0, 121.0, 122.0, 140.0, 141.0, 142.0],
            step_minutes=60,
        )
        zone = find_h1_fvg_zone(candles, Direction.LONG, min_size=1.0)
        assert zone is not None
        # the later gap sits higher than the earlier one
        assert zone.bottom > 120.0

    def test_bearish_gap_is_supply(self):
        candles = make_candles(
            [140.0, 139.0, 138.0, 120.0, 119.0, 118.0, 117.0, 116.0],
            step_minutes=60,
        )
        zone = find_h1_fvg_zone(candles, Direction.SHORT, min_size=1.0)
        assert zone is not None and zone.is_demand is False and zone.kind == "FVG"

    def test_zone_carries_the_gap_formation_candle(self):
        candles = _gap_up()
        zone = find_h1_fvg_zone(candles, Direction.LONG, min_size=1.0)
        assert zone is not None
        assert candles[zone.pivot_index].timestamp == zone.timestamp
