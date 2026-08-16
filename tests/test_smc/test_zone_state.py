"""zone_pinged (per-block list) and zone_muted plumbing (spec §1.2, §1.4)."""

from datetime import datetime, timedelta, timezone

from app.services.smc.db import Database
from app.services.smc.state import WatcherState

BLOCK = "2026-08-16/Frankfurt-London"
NEXT_BLOCK = "2026-08-16/New York"


def _state(tmp_path, name="t.db"):
    return WatcherState(Database(str(tmp_path / name)))


class TestZonePingedRecords:
    def test_records_and_matches_the_same_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert state.zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "long", BLOCK
        )

    def test_overlapping_zone_counts_as_the_same_zone(self, tmp_path):
        """D2: the plan is recomputed every 5 minutes and bounds drift."""
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert state.zone_already_pinged(
            "ETHUSD", 3135.0, 3141.0, "long", BLOCK
        )

    def test_non_overlapping_zone_is_a_different_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "ETHUSD", 3200.0, 3210.0, "long", BLOCK
        )

    def test_opposite_direction_is_a_different_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "short", BLOCK
        )

    def test_new_block_forgets_the_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "long", NEXT_BLOCK
        )

    def test_other_pairs_are_independent(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "USDCAD", 3131.0, 3138.0, "long", BLOCK
        )

    def test_two_zones_in_one_block_both_remembered(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        state.remember_zone_ping("ETHUSD", 3200.0, 3210.0, "long", BLOCK)
        assert state.zone_already_pinged("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert state.zone_already_pinged("ETHUSD", 3200.0, 3210.0, "long", BLOCK)

    def test_writing_a_new_block_prunes_the_old_entries(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        state.remember_zone_ping("ETHUSD", 3200.0, 3210.0, "long", NEXT_BLOCK)
        assert state.zone_pinged["ETHUSD"] == [
            [3200.0, 3210.0, "long", NEXT_BLOCK]
        ]

    def test_round_trips_through_the_db(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert _state(tmp_path).zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "long", BLOCK
        )

    def test_legacy_shapes_are_dropped_on_load(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.kv_set("zone_pinged", {
            "ETHUSD": True,                                  # pre-auto-plan
            "USDJPY": [3131.0, 3138.0, "long", "2026-08-11"],  # flat 4-list
            "USDCAD": [[1.39448, 1.39584, "short", BLOCK]],    # current
        })
        state = WatcherState(db)
        assert "ETHUSD" not in state.zone_pinged
        assert "USDJPY" not in state.zone_pinged
        assert state.zone_pinged["USDCAD"] == [
            [1.39448, 1.39584, "short", BLOCK]
        ]

    def test_set_profile_still_clears_the_key(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        state.set_profile("ETHUSD", "conservative")
        assert "ETHUSD" not in state.zone_pinged


class TestZoneMute:
    def test_mute_is_live_before_the_deadline(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        label = state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        assert state.zone_muted_until("USDCAD", now) == label

    def test_mute_expires(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        assert state.zone_muted_until(
            "USDCAD", now + timedelta(hours=3)
        ) is None

    def test_unmuted_pair_reads_none(self, tmp_path):
        state = _state(tmp_path)
        assert state.zone_muted_until("ETHUSD") is None

    def test_poisoned_deadline_reads_as_unmuted(self, tmp_path):
        state = _state(tmp_path)
        state.zone_muted["ETHUSD"] = "not-a-timestamp"
        assert state.zone_muted_until("ETHUSD") is None

    def test_mute_round_trips_through_the_db(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        assert _state(tmp_path).zone_muted_until("USDCAD", now) is not None

    def test_clear_returns_the_freed_pairs(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        state.mute_zone_alerts("ETHUSD", now + timedelta(hours=2))
        assert sorted(state.clear_zone_mutes()) == ["ETHUSD", "USDCAD"]
        assert state.zone_muted == {}

    def test_keys_are_upper_cased(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("usdcad", now + timedelta(hours=2))
        assert state.zone_muted_until("USDCAD", now) is not None
