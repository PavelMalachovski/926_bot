from app.services.smc.profiles import (
    PROFILES, CONSERVATIVE, AGGRESSIVE, get_profile,
)


def test_conservative_is_todays_behaviour():
    assert CONSERVATIVE.fvg_size_factor == 1.0
    assert CONSERVATIVE.max_zone_touches == 0
    assert CONSERVATIVE.fvg_day_scope is False
    assert CONSERVATIVE.allow_h4_choch_entry is False


def test_aggressive_relaxes_all_four():
    assert AGGRESSIVE.allow_h4_choch_entry is True
    assert AGGRESSIVE.max_zone_touches >= 1
    assert AGGRESSIVE.fvg_day_scope is True
    assert AGGRESSIVE.fvg_size_factor < 1.0


def test_get_profile_falls_back_to_conservative():
    assert get_profile("nonsense") is CONSERVATIVE
    assert get_profile("aggressive") is AGGRESSIVE
    assert set(PROFILES) == {"conservative", "aggressive"}


def test_get_profile_handles_empty_and_none():
    assert get_profile("") is CONSERVATIVE
    assert get_profile(None) is CONSERVATIVE
