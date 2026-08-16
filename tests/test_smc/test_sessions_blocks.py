"""Session block identity and mute deadlines (spec 2026-08-16 §1.1, §1.4)."""

from datetime import datetime, timezone

from app.services.smc.sessions import (
    PRAGUE,
    block_mute_deadline,
    prague_hhmm,
    session_block,
)


def _utc(y, m, d, hh, mm):
    """Build a UTC instant from a Prague wall-clock time (DST-aware)."""
    local = PRAGUE.localize(datetime(y, m, d, hh, mm))
    return local.astimezone(timezone.utc)


class TestSessionBlock:
    def test_london_block_id(self):
        assert session_block(_utc(2026, 8, 16, 9, 0)) == (
            "2026-08-16/Frankfurt-London"
        )

    def test_ny_block_id(self):
        assert session_block(_utc(2026, 8, 16, 15, 0)) == "2026-08-16/New York"

    def test_blocks_differ_across_the_1400_split(self):
        assert session_block(_utc(2026, 8, 16, 13, 59)) != session_block(
            _utc(2026, 8, 16, 14, 1)
        )

    def test_same_block_all_afternoon(self):
        assert session_block(_utc(2026, 8, 16, 14, 5)) == session_block(
            _utc(2026, 8, 16, 18, 25)
        )

    def test_blocks_differ_across_days(self):
        assert session_block(_utc(2026, 8, 16, 9, 0)) != session_block(
            _utc(2026, 8, 17, 9, 0)
        )

    def test_none_before_the_open(self):
        assert session_block(_utc(2026, 8, 16, 7, 0)) is None

    def test_none_after_the_close(self):
        assert session_block(_utc(2026, 8, 16, 18, 35)) is None


class TestBlockMuteDeadline:
    def test_non_last_block_expires_at_its_own_end(self):
        assert block_mute_deadline("2026-08-16/Frankfurt-London") == _utc(
            2026, 8, 16, 14, 0
        )

    def test_last_block_expires_at_the_next_days_open(self):
        assert block_mute_deadline("2026-08-16/New York") == _utc(
            2026, 8, 17, 8, 0
        )

    def test_unknown_window_name_returns_none(self):
        assert block_mute_deadline("2026-08-16/Tokyo") is None

    def test_malformed_id_returns_none(self):
        assert block_mute_deadline("garbage") is None

    def test_deadline_is_utc_aware(self):
        deadline = block_mute_deadline("2026-08-16/Frankfurt-London")
        assert deadline.tzinfo is not None


class TestPragueHhmm:
    def test_renders_prague_wall_clock(self):
        assert prague_hhmm(_utc(2026, 8, 16, 14, 0)) == "14:00"

    def test_winter_offset(self):
        assert prague_hhmm(_utc(2026, 1, 16, 9, 5)) == "09:05"
