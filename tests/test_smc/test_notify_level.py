"""Tests for /notify: a global notification level replacing the retired
per-pair /strategy picker (owner decision 2026-08-12, Phase 2 Task 4b).

Levels: "all" (star loud + regular quiet), "star" (star only, regular setups
logged not sent), "mute" (no setup alerts at all). Mute affects setup
alerts only -- the 07:45 digest, plan-zone alerts and Rule 0.4/9 warnings
keep flowing regardless of the level (covered in test_multipair.py /
test_poison_state.py, unchanged by this feature).
"""

import pytest

from app.services.smc.db import Database
from app.services.smc.state import NOTIFY_LEVELS, WatcherState
from app.services.smc.telegram_bot import HELP_TEXT, TelegramCommandBot


# --------------------------------------------------------------------- state


class TestNotifyLevelState:
    def test_defaults_to_all(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        assert state.notify_level == "all"

    def test_set_and_persists_across_reload(self, tmp_path):
        db_path = str(tmp_path / "s.db")
        state = WatcherState(Database(db_path))

        state.set_notify_level("star")

        assert state.notify_level == "star"
        reloaded = WatcherState(Database(db_path))
        assert reloaded.notify_level == "star"

    def test_all_three_levels_are_accepted(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        for level in NOTIFY_LEVELS:
            state.set_notify_level(level)
            assert state.notify_level == level

    def test_rejects_unknown_level(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))

        with pytest.raises(ValueError):
            state.set_notify_level("loud")

        assert state.notify_level == "all"  # unchanged by the rejected call


# ---------------------------------------------------------------- bot helper


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


def _keyboard_by_callback(payload):
    rows = payload["reply_markup"]["inline_keyboard"]
    return {row[0]["callback_data"]: row[0]["text"] for row in rows}


# ------------------------------------------------------------- /notify command


class TestNotifyCommand:
    @pytest.mark.asyncio
    async def test_renders_three_options_with_the_current_one_marked(
        self, tmp_path
    ):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        state.set_notify_level("star")
        bot = _bot(state)

        await bot._handle_command("/notify")

        method, payload = bot.api_calls[-1]
        assert method == "sendMessage"
        by_callback = _keyboard_by_callback(payload)
        assert set(by_callback) == {"notify_all", "notify_star", "notify_mute"}
        assert "✅" in by_callback["notify_star"]
        assert "✅" not in by_callback["notify_all"]
        assert "✅" not in by_callback["notify_mute"]

    @pytest.mark.asyncio
    async def test_default_level_marks_all(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        bot = _bot(state)

        await bot._handle_command("/notify")

        _, payload = bot.api_calls[-1]
        by_callback = _keyboard_by_callback(payload)
        assert "✅" in by_callback["notify_all"]


# ------------------------------------------------------------ /notify callback


class TestNotifyCallback:
    @pytest.mark.asyncio
    async def test_switches_level_and_edits_the_message(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        bot = _bot(state)
        callback = {
            "id": "cb1",
            "data": "notify_mute",
            "message": {"chat": {"id": 1}, "message_id": 5},
        }

        await bot._handle_callback(callback)

        assert state.notify_level == "mute"
        methods = [m for m, _ in bot.api_calls]
        assert "editMessageReplyMarkup" in methods
        assert "answerCallbackQuery" in methods
        edit_payload = next(
            p for m, p in bot.api_calls if m == "editMessageReplyMarkup"
        )
        by_callback = _keyboard_by_callback(edit_payload)
        assert "✅" in by_callback["notify_mute"]
        assert "✅" not in by_callback["notify_all"]

    @pytest.mark.asyncio
    async def test_switching_twice_moves_the_mark(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        bot = _bot(state)

        await bot._handle_callback({
            "id": "cb1", "data": "notify_star",
            "message": {"chat": {"id": 1}, "message_id": 5},
        })
        await bot._handle_callback({
            "id": "cb2", "data": "notify_all",
            "message": {"chat": {"id": 1}, "message_id": 5},
        })

        assert state.notify_level == "all"
        edit_payload = [
            p for m, p in bot.api_calls if m == "editMessageReplyMarkup"
        ][-1]
        by_callback = _keyboard_by_callback(edit_payload)
        assert "✅" in by_callback["notify_all"]

    @pytest.mark.asyncio
    async def test_unknown_level_is_rejected_without_changing_state(
        self, tmp_path
    ):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        bot = _bot(state)
        callback = {
            "id": "cb1",
            "data": "notify_bogus",
            "message": {"chat": {"id": 1}, "message_id": 5},
        }

        await bot._handle_callback(callback)

        assert state.notify_level == "all"


# ----------------------------------------------------------------- slash menu


class TestSlashMenu:
    @pytest.mark.asyncio
    async def test_notify_registered_and_strategy_removed(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "s.db")))
        bot = _bot(state)

        await bot._setup_bot_profile()

        commands_call = next(
            p for m, p in bot.api_calls if m == "setMyCommands"
        )
        names = [c["command"] for c in commands_call["commands"]]
        assert "notify" in names
        assert "strategy" not in names

    def test_help_text_mentions_notify_not_strategy(self):
        assert "/notify" in HELP_TEXT
        assert "/strategy" not in HELP_TEXT
