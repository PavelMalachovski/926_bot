"""Tests for the taken-trade cooldown: after 'Took it', mute the pair 4h."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.smc.db import Database


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.telegram, "bot_token", "123:dummy")
    monkeypatch.setattr(settings.telegram, "chat_id", "1")
    monkeypatch.setattr(settings.smc, "chat_id", None)
    monkeypatch.setenv("SMC_DB_FILE", str(tmp_path / "smc.db"))
    import importlib

    import smc_watcher

    importlib.reload(smc_watcher)
    monkeypatch.setattr(smc_watcher, "DB_FILE", str(tmp_path / "smc.db"))
    return smc_watcher.Watcher()


def _record_signal(watcher, pair="USDJPY"):
    signal = {
        "id": "sig1",
        "pair": pair,
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "rr": 2.0,
        "session": "New York",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "expires_at": None,
        "status": "pending",
        "filled_at": None,
        "resolved_at": None,
        "checked_until": None,
        "taken": None,
        "message_id": None,
        "alert_text": None,
    }
    watcher.journal.signals.append(signal)
    watcher.journal.save()
    return signal


@pytest.mark.asyncio
async def test_taken_sets_cooldown(watcher):
    _record_signal(watcher, "USDJPY")
    reply = await watcher.mark_trade("sig1", taken=True)
    assert "muted for 4h" in reply
    left = watcher._cooldown_left("USDJPY")
    assert left is not None and left.startswith("3h")  # ~3h59m


@pytest.mark.asyncio
async def test_skipped_sets_no_cooldown(watcher):
    _record_signal(watcher, "USDJPY")
    await watcher.mark_trade("sig1", taken=False)
    assert watcher._cooldown_left("USDJPY") is None


def test_expired_cooldown_is_cleared(watcher):
    past = (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()
    watcher.state.pair_cooldown["GBPUSD"] = past
    assert watcher._cooldown_left("GBPUSD") is None
    assert "GBPUSD" not in watcher.state.pair_cooldown  # pruned


def test_cooldown_persists_across_reload(watcher, tmp_path):
    watcher.state.pair_cooldown["ETHUSD"] = (
        datetime.now(tz=timezone.utc) + timedelta(hours=2)
    ).isoformat()
    watcher.state.save()
    from app.services.smc.state import WatcherState

    reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
    assert "ETHUSD" in reloaded.pair_cooldown
