"""D4: the order block wins; the imbalance is the fallback (spec §2.1)."""

from datetime import datetime, timezone

import app.services.smc.structure as structure
from app.services.smc.models import Direction, Zone
from tests.test_smc.helpers import H1_PULLBACK_CLOSES, make_candles


def _zone(kind, bottom=100.0, top=101.0):
    return Zone(
        bottom=bottom, top=top, is_demand=True, pivot_index=3,
        timestamp=datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc), kind=kind,
    )


class TestFindZoneOfInterest:
    def test_order_block_wins_when_both_qualify(self, monkeypatch):
        monkeypatch.setattr(structure, "find_h1_zone", lambda *a, **k: _zone("OB"))
        monkeypatch.setattr(
            structure, "find_h1_fvg_zone", lambda *a, **k: _zone("FVG", 90.0, 91.0)
        )
        zone = structure.find_zone_of_interest(
            [], Direction.LONG, min_size=1.0, max_touches=0
        )
        assert zone is not None and zone.kind == "OB"

    def test_imbalance_used_when_no_order_block(self, monkeypatch):
        monkeypatch.setattr(structure, "find_h1_zone", lambda *a, **k: None)
        monkeypatch.setattr(
            structure, "find_h1_fvg_zone", lambda *a, **k: _zone("FVG", 90.0, 91.0)
        )
        zone = structure.find_zone_of_interest(
            [], Direction.LONG, min_size=1.0, max_touches=0
        )
        assert zone is not None and zone.kind == "FVG"

    def test_none_when_neither_qualifies(self, monkeypatch):
        monkeypatch.setattr(structure, "find_h1_zone", lambda *a, **k: None)
        monkeypatch.setattr(structure, "find_h1_fvg_zone", lambda *a, **k: None)
        assert structure.find_zone_of_interest(
            [], Direction.LONG, min_size=1.0, max_touches=0
        ) is None

    def test_real_candles_still_find_the_order_block(self):
        """No mocks: the existing H1 pullback fixture must behave as before."""
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        before = structure.find_h1_zone(h1, Direction.LONG, max_touches=0)
        after = structure.find_zone_of_interest(
            h1, Direction.LONG, min_size=1.0, max_touches=0
        )
        assert before is not None
        assert after is not None
        assert (after.bottom, after.top, after.kind) == (
            before.bottom, before.top, "OB",
        )
