"""The 🔕 button under a zone alert: keyboard, callback, watcher hook.

The button is anchored to the block the ALERT was sent in, not to the
press moment (owner decision 2026-08-16): a 13:55 alert's button must not
silence the entire evening session just because the owner pressed it at
14:02. `block_mute_deadline` is monkeypatched in most of these tests so the
assertions do not depend on the real wall clock ever catching up with a
hardcoded calendar date (the earlier time-bomb mistake this branch already
fixed once, for the status line).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.services.smc.notifier import zone_alert_keyboard
from app.services.smc.sessions import PRAGUE, prague_hhmm
from app.services.smc.state import WatcherState
from app.services.smc.telegram_bot import TelegramCommandBot

BLOCK = "2026-08-16/Frankfurt-London"


def _utc(hh, mm, day=16):
    return PRAGUE.localize(datetime(2026, 8, day, hh, mm)).astimezone(
        timezone.utc
    )


class TestZoneAlertKeyboard:
    def test_single_mute_button(self):
        kb = zone_alert_keyboard("USDCAD", "14:00", BLOCK)
        assert kb == {"inline_keyboard": [[{
            "text": "🔕 Mute USDCAD zone alerts till 14:00",
            "callback_data": f"zmute_USDCAD_{BLOCK}",
        }]]}


class _State:
    mute_zone_alerts = WatcherState.mute_zone_alerts
    zone_muted_until = WatcherState.zone_muted_until

    def __init__(self):
        self.zone_muted = {}
        self.paused = False

    def save(self):
        pass


class _Bot(TelegramCommandBot):
    def __init__(self, on_zone_mute):
        self.calls = []
        self.state = _State()
        self.owner_chat_id = "1"
        self.on_zone_mute = on_zone_mute
        self.trade_journal = None
        self.on_trade_mark = None
        self.on_plan = None
        self.on_stored_plan = None

    async def _api(self, method, **payload):
        self.calls.append((method, payload))
        return {}


class TestZmuteCallback:
    def _callback(self, data=f"zmute_USDCAD_{BLOCK}"):
        return {
            "id": "cb1",
            "data": data,
            "message": {"chat": {"id": 1}, "message_id": 42},
        }

    def test_calls_the_hook_with_pair_and_block_and_answers(self):
        calls = []

        async def hook(key, block_id):
            calls.append((key, block_id))
            return "14:00"

        bot = _Bot(hook)
        asyncio.run(bot._handle_callback(self._callback()))
        assert calls == [("USDCAD", BLOCK)]
        answered = [c for c in bot.calls if c[0] == "answerCallbackQuery"]
        assert answered[0][1]["text"] == "USDCAD zone alerts muted till 14:00"

    def test_replaces_the_keyboard_with_an_inert_button(self):
        async def hook(key, block_id):
            return "14:00"

        bot = _Bot(hook)
        asyncio.run(bot._handle_callback(self._callback()))
        edits = [c for c in bot.calls if c[0] == "editMessageReplyMarkup"]
        assert edits[0][1]["reply_markup"] == {"inline_keyboard": [[{
            "text": "🔕 Muted till 14:00", "callback_data": "noop",
        }]]}

    def test_legacy_payload_with_no_block_part_still_works(self):
        async def hook(key, block_id):
            assert block_id is None
            return "14:00"

        bot = _Bot(hook)
        asyncio.run(bot._handle_callback(self._callback(data="zmute_USDCAD")))
        answered = [c for c in bot.calls if c[0] == "answerCallbackQuery"]
        assert answered[0][1]["text"] == "USDCAD zone alerts muted till 14:00"

    def test_ended_block_reports_nothing_muted_and_answers(self):
        async def hook(key, block_id):
            return None

        bot = _Bot(hook)
        asyncio.run(bot._handle_callback(self._callback()))
        answered = [c for c in bot.calls if c[0] == "answerCallbackQuery"]
        assert answered, "the callback query must always be answered"
        assert "nothing muted" in answered[0][1]["text"].lower()
        edits = [c for c in bot.calls if c[0] == "editMessageReplyMarkup"]
        button = edits[0][1]["reply_markup"]["inline_keyboard"][0][0]
        assert button["callback_data"] == "noop"


class TestWatcherHook:
    def _watcher(self):
        from smc_watcher import Watcher

        w = Watcher.__new__(Watcher)
        w.state = _State()
        return w

    def test_live_block_stores_the_deadline(self, monkeypatch):
        import smc_watcher as mod

        w = self._watcher()
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        monkeypatch.setattr(mod, "block_mute_deadline", lambda block: future)

        result = asyncio.run(w.mark_zone_mute("USDCAD", BLOCK))

        assert result == prague_hhmm(future)
        now = datetime.now(tz=timezone.utc)
        assert w.state.zone_muted_until("USDCAD", now) == prague_hhmm(future)

    def test_ended_block_stores_nothing_and_returns_none(self, monkeypatch):
        import smc_watcher as mod

        w = self._watcher()
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        monkeypatch.setattr(mod, "block_mute_deadline", lambda block: past)

        result = asyncio.run(w.mark_zone_mute("USDCAD", BLOCK))

        assert result is None
        assert w.state.zone_muted == {}

    def test_unknown_block_id_returns_none(self, monkeypatch):
        import smc_watcher as mod

        w = self._watcher()
        monkeypatch.setattr(mod, "block_mute_deadline", lambda block: None)

        result = asyncio.run(w.mark_zone_mute("USDCAD", "garbage"))

        assert result is None
        assert w.state.zone_muted == {}

    def test_legacy_none_block_id_falls_back_to_the_press_block(
        self, monkeypatch
    ):
        import smc_watcher as mod

        w = self._watcher()
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        seen = []

        def fake_session_block(now):
            seen.append(now)
            return BLOCK

        monkeypatch.setattr(mod, "session_block", fake_session_block)
        monkeypatch.setattr(mod, "block_mute_deadline", lambda block: future)

        result = asyncio.run(w.mark_zone_mute("USDCAD", None))

        assert result == prague_hhmm(future)
        assert seen, "the fallback must consult session_block"


class TestAlertCarriesTheButton:
    def test_zone_alert_is_sent_with_the_mute_keyboard(self, monkeypatch):
        from tests.test_smc.test_zone_dedup import _result, _watcher

        w = _watcher(monkeypatch)
        monkeypatch.setattr(settings.smc, "zone_ping", True)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        _text, markup = w.notifier.sent[0]
        assert markup["inline_keyboard"][0][0]["callback_data"] == (
            f"zmute_ETHUSD_{BLOCK}"
        )


class TestButtonLabelMatchesPressDeadline:
    """The label promises a deadline; pressing the button must produce
    exactly that deadline (finding 1's whole point). Both call sites go
    through the same `block_mute_deadline`, monkeypatched once here so the
    equivalence does not depend on the real wall clock."""

    def test_alert_label_matches_what_a_press_would_produce(self, monkeypatch):
        import smc_watcher as mod
        from tests.test_smc.test_zone_dedup import _result, _watcher

        future = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(mod, "block_mute_deadline", lambda block: future)
        w = _watcher(monkeypatch)
        monkeypatch.setattr(settings.smc, "zone_ping", True)

        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))

        _text, markup = w.notifier.sent[0]
        button = markup["inline_keyboard"][0][0]
        label_deadline = button["text"].rsplit(" ", 1)[-1]
        block_id = button["callback_data"][len("zmute_ETHUSD_"):]

        pressed = asyncio.run(w.mark_zone_mute("ETHUSD", block_id))

        assert pressed == label_deadline == prague_hhmm(future)


class _UnmuteState(_State):
    clear_zone_mutes = WatcherState.clear_zone_mutes


class _CommandBot(_Bot):
    def __init__(self):
        super().__init__(None)
        self.state = _UnmuteState()
        self.sent = []

    async def send(self, text, reply_markup=None):
        self.sent.append(text)
        return 1


class TestUnmuteCommand:
    def test_unmute_frees_every_pair(self):
        bot = _CommandBot()
        bot.state.mute_zone_alerts("USDCAD", _utc(14, 0))
        bot.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        asyncio.run(bot._handle_command("/unmute"))
        assert bot.state.zone_muted == {}
        assert "USDCAD" in bot.sent[0] and "ETHUSD" in bot.sent[0]

    def test_unmute_with_nothing_muted(self):
        bot = _CommandBot()
        asyncio.run(bot._handle_command("/unmute"))
        assert "No pairs are muted" in bot.sent[0]


class TestStatusLine:
    def test_status_lists_muted_pairs(self, monkeypatch):
        from smc_watcher import Watcher

        w = Watcher.__new__(Watcher)
        w.state = _UnmuteState()
        w.state.pairs = ["ETHUSD", "USDCAD"]
        w.state.pair_cooldown = {}
        w.state.notify_level = "all"
        w.last_results = {}
        # Compute a deadline a bit in the future, so the test passes
        # whenever it runs (not a time bomb like hardcoded 23:59).
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        expected_hhmm = prague_hhmm(future)
        w.state.mute_zone_alerts("USDCAD", future)
        assert f"🔕 Zone alerts muted: USDCAD (till {expected_hhmm})" in w.status_text()
