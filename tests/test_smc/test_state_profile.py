from app.services.smc.db import Database
from app.services.smc.state import WatcherState


def test_set_profile_persists_and_clears_dedup(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    state = WatcherState(db)
    state.last_setup["ETHUSD"] = "some-fingerprint"
    state.zone_pinged["ETHUSD"] = True
    state.save()

    state.set_profile("ETHUSD", "aggressive")
    assert state.pair_profile["ETHUSD"] == "aggressive"
    assert "ETHUSD" not in state.last_setup
    assert state.zone_pinged.get("ETHUSD", False) is False

    # survives a reload
    state2 = WatcherState(Database(str(tmp_path / "s.db")))
    assert state2.pair_profile["ETHUSD"] == "aggressive"
