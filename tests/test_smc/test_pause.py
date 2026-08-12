"""Tests for /pause–/resume: global mute of scheduler cycles until resumed."""

import pytest

from app.core.config import settings
from app.services.smc.db import Database
from app.services.smc.telegram_bot import TelegramCommandBot


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


class TestPausedWatcher:
    @pytest.mark.asyncio
    async def test_run_cycle_is_noop_while_paused(self, watcher, monkeypatch):
        watcher.state.pairs = ["ETHUSD"]
        watcher.state.set_paused(True)

        async def _fail(*args, **kwargs):
            raise AssertionError("must not check pairs while paused")

        monkeypatch.setattr(watcher, "check_pair", _fail)
        summary = await watcher.run_cycle()
        assert "paused" in summary.lower() and "/resume" in summary

    def test_paused_persists_across_reload(self, watcher, tmp_path):
        watcher.state.set_paused(True)
        from app.services.smc.state import WatcherState

        reloaded = WatcherState(Database(str(tmp_path / "smc.db")))
        assert reloaded.paused is True
        reloaded.set_paused(False)
        assert WatcherState(Database(str(tmp_path / "smc.db"))).paused is False

    def test_status_shows_paused(self, watcher):
        watcher.state.set_paused(True)
        assert "PAUSED" in watcher.status_text()
        watcher.state.set_paused(False)
        assert "PAUSED" not in watcher.status_text()

    def test_status_shows_notify_level_not_retired_profiles_picker(
        self, watcher
    ):
        """Task 6 fix wave: the per-pair /strategy picker was retired
        (owner decision 2026-08-12, global notify_level replaced it) but
        /status kept printing a stale "Profiles:" line. It now reports the
        current notification level instead."""
        watcher.state.set_notify_level("star")
        text = watcher.status_text()
        assert "Notify: star" in text
        assert "Profiles:" not in text


class _State:
    def __init__(self):
        self.paused = False
        self.pairs = ["ETHUSD"]

    def set_paused(self, paused):
        self.paused = paused


def _bot(state):
    async def run_cycle():
        return "ok"

    bot = TelegramCommandBot(
        bot_token="123:dummy",
        owner_chat_id="1",
        state=state,
        run_cycle=run_cycle,
        status_text=lambda: "status",
    )
    bot.api_calls = []

    async def _api(method, http_timeout=35.0, **payload):
        bot.api_calls.append((method, payload))
        return {"ok": True}

    bot._api = _api
    return bot


class TestPauseCommands:
    @pytest.mark.asyncio
    async def test_pause_command_sets_flag_and_offers_resume(self):
        state = _State()
        bot = _bot(state)
        await bot._handle_command("/pause")
        assert state.paused is True
        method, payload = bot.api_calls[-1]
        assert method == "sendMessage" and "Paused" in payload["text"]
        buttons = payload["reply_markup"]["inline_keyboard"][0]
        assert buttons[0]["callback_data"] == "resume"

    @pytest.mark.asyncio
    async def test_resume_command_clears_flag(self):
        state = _State()
        state.paused = True
        bot = _bot(state)
        await bot._handle_command("/resume")
        assert state.paused is False
        method, payload = bot.api_calls[-1]
        assert method == "sendMessage" and "Resumed" in payload["text"]

    @pytest.mark.asyncio
    async def test_resume_callback_clears_flag(self):
        state = _State()
        state.paused = True
        bot = _bot(state)
        callback = {
            "id": "cb1",
            "data": "resume",
            "message": {"chat": {"id": 1}, "message_id": 7},
        }
        await bot._handle_callback(callback)
        assert state.paused is False
        methods = [m for m, _ in bot.api_calls]
        assert "editMessageReplyMarkup" in methods
        assert "answerCallbackQuery" in methods

    @pytest.mark.asyncio
    async def test_pause_message_is_current_help(self):
        from app.services.smc.telegram_bot import HELP_TEXT

        assert "/pause" in HELP_TEXT and "/resume" in HELP_TEXT


class TestNewDefaults:
    def test_zone_ping_default_on(self, monkeypatch):
        monkeypatch.delenv("SMC_ZONE_PING", raising=False)
        from app.core.config import SMCSettings

        assert SMCSettings().zone_ping is True

    def test_chart_candles_default_doubled(self):
        from app.services.smc import chart

        assert chart.CHART_CANDLES == 384


def test_paused_flag_default_false(tmp_path):
    from app.services.smc.state import WatcherState

    state = WatcherState(Database(str(tmp_path / "fresh.db")))
    assert state.paused is False
