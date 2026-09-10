"""Bot-facing text in two languages (owner request 2026-09-10).

Russian is the default, English stays switchable from the ⚙️ /settings
menu. These tests pin the catalog's integrity (every key the code uses is
translated, every translation carries the same placeholders as its key),
the Russian rendering of the main messages, the persisted language choice
and the /settings hub that changes it.
"""

import ast
import glob
import string
from datetime import datetime, timezone

import pytest

from app.services.smc import i18n
from app.services.smc.db import Database
from app.services.smc.i18n import RU, t
from app.services.smc.state import WatcherState
from app.services.smc.telegram_bot import TelegramCommandBot, help_text

SOURCE_FILES = sorted(glob.glob("app/services/smc/*.py")) + ["smc_watcher.py"]


def _placeholders(text: str):
    return {
        name for _, name, _, _ in string.Formatter().parse(text) if name is not None
    }


def _keys_in_code():
    keys = set()
    for path in SOURCE_FILES:
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


class TestCatalog:
    def test_every_literal_key_in_code_is_translated(self):
        missing = sorted(k for k in _keys_in_code() if k not in RU)
        assert not missing, f"untranslated t() keys: {missing[:5]}"

    def test_translations_keep_their_placeholders(self):
        broken = [
            k for k, v in RU.items() if _placeholders(k) != _placeholders(v)
        ]
        assert not broken, f"placeholder mismatch: {broken[:5]}"

    def test_translations_are_not_empty(self):
        assert all(v.strip() for v in RU.values())

    def test_dynamic_labels_are_translated(self):
        # keys reached through variables rather than literals
        for key in (
            "discount", "premium", "equilibrium",
            "too small", "over half filled", "closed through",
            "from an earlier session", "agree", "caution", "against",
            "uptrend", "downtrend", "flat",
            "   ▶️ price is inside the imbalance right now",
            "   ▶️ price is inside the order block right now",
            "   ▶️ market entry — price is at the CHoCH",
            "all setups", "⭐ only", "no setup alerts",
            "room", "sweep", "pd", "stale", "trend", "imbalance",
        ):
            assert key in RU, key


class TestLanguageSwitch:
    def test_english_returns_the_key(self):
        i18n.set_language("en")
        assert t("Demand") == "Demand"
        assert t("{pair} marked as skipped", pair="ETHUSD") == "ETHUSD marked as skipped"

    def test_russian_translates_and_formats(self):
        i18n.set_language("ru")
        assert t("{pair} marked as skipped", pair="ETHUSD") == "ETHUSD отмечен как пропущенный"
        assert t("bullish") == "бычий"

    def test_unknown_key_falls_back_to_english(self):
        i18n.set_language("ru")
        assert t("not in the catalog {x}", x=1) == "not in the catalog 1"

    def test_normalize_and_bad_values(self, monkeypatch):
        assert i18n.normalize_language("RU") == "ru"
        assert i18n.normalize_language("en-GB") == "en"
        assert i18n.normalize_language("de") is None
        assert i18n.normalize_language(None) is None
        # garbage falls back to the configured default rather than raising
        from app.core.config import settings

        monkeypatch.setattr(settings.smc, "language", "ru")
        assert i18n.set_language("klingon") == "ru"

    def test_default_language_is_russian(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings.smc, "language", "ru")
        assert i18n.set_language(None) == "ru"

    def test_touches_plural_forms(self):
        from app.services.smc.notifier import touches_label

        i18n.set_language("ru")
        assert touches_label(1) == "1 касание"
        assert touches_label(3) == "3 касания"
        assert touches_label(5) == "5 касаний"
        assert touches_label(11) == "11 касаний"
        assert touches_label(22) == "22 касания"
        i18n.set_language("en")
        assert touches_label(1) == "1 touch"
        assert touches_label(2) == "2 touches"


class TestRussianRendering:
    def test_alert_card_in_russian(self):
        from tests.test_smc.test_visuals import _approved_result
        from app.services.smc.notifier import format_result

        i18n.set_language("ru")
        text = format_result(_approved_result(), in_plan=True)
        assert "СЕТАП ГОТОВ — ETHUSD · LONG" in text
        assert "Вход по рынку" in text
        assert "из утреннего плана" in text
        assert "справка · отложенный ордер живёт до конца сессии" in text
        # trading vocabulary stays Latin, only prose is translated
        assert "FVG" in text and "SL" in text and "LONG" in text
        assert "{" not in text  # every placeholder was filled

    def test_english_card_is_unchanged_by_the_catalog(self):
        from tests.test_smc.test_visuals import _approved_result
        from app.services.smc.notifier import format_result

        i18n.set_language("en")
        text = format_result(_approved_result(), in_plan=True)
        assert "SETUP READY — ETHUSD · LONG" in text
        assert "from this morning's plan" in text

    def test_engine_reasons_follow_the_language(self):
        from app.services.smc.engine import TripleSyncEngine
        from app.services.smc.models import AnalysisResult, Verdict
        from tests.test_smc.helpers import (
            H1_PULLBACK_CLOSES, H4_UPTREND_CLOSES, m5_long_trigger, make_candles,
        )

        engine = TripleSyncEngine(
            min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0,
        )
        h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
        h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
        # cut the M5 leg before the CHoCH: Rule 3 stops with a WATCH reason
        m5 = m5_long_trigger()[:-6]

        def fresh() -> AnalysisResult:
            return AnalysisResult(
                symbol="ETHUSD", verdict=Verdict.SKIP,
                checked_at=datetime.now(tz=timezone.utc), price_decimals=2,
            )

        i18n.set_language("ru")
        result = engine.evaluate(h4=h4, h1=h1, m5=m5, result=fresh())
        i18n.set_language("en")
        result_en = engine.evaluate(h4=h4, h1=h1, m5=m5, result=fresh())
        assert result.verdict == result_en.verdict
        assert result.reasons and result_en.reasons
        assert result.reasons[0] != result_en.reasons[0]
        assert any(ord(c) > 1000 for c in result.reasons[0]), result.reasons
        assert all(ord(c) < 1000 for c in result_en.reasons[0]), result_en.reasons
        assert result.verdict in (Verdict.WATCH, Verdict.SKIP)

    def test_chart_renders_cyrillic_labels(self):
        from tests.test_smc.test_visuals import _approved_result
        from app.services.smc.chart import render_setup_chart

        i18n.set_language("ru")
        png = render_setup_chart(_approved_result())
        assert png and png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_help_text_in_both_languages(self):
        i18n.set_language("ru")
        assert "/settings — язык, пары" in help_text()
        i18n.set_language("en")
        assert "/settings — language, pairs" in help_text()

    def test_ai_read_asks_for_the_language(self):
        from app.services.smc.ai_read import LANGUAGE_INSTRUCTION

        assert "Russian" in LANGUAGE_INSTRUCTION["ru"]
        assert "English" in LANGUAGE_INSTRUCTION["en"]


class TestStatePersistence:
    def test_language_round_trips_through_the_db(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        state = WatcherState(db)
        assert state.language is None  # never chosen -> env default applies
        assert state.set_language("EN") == "en"
        assert WatcherState(Database(str(tmp_path / "smc.db"))).language == "en"

    def test_poisoned_language_reads_as_unset(self, tmp_path):
        db = Database(str(tmp_path / "smc.db"))
        db.kv_set("language", "klingon")
        assert WatcherState(db).language is None

    def test_set_language_rejects_unknown(self, tmp_path):
        state = WatcherState(Database(str(tmp_path / "smc.db")))
        with pytest.raises(ValueError):
            state.set_language("de")


def _bot(tmp_path) -> TelegramCommandBot:
    state = WatcherState(Database(str(tmp_path / "smc.db")))

    async def run_cycle():
        return "ok"

    bot = TelegramCommandBot(
        bot_token="123:dummy", owner_chat_id="1", state=state,
        run_cycle=run_cycle, status_text=lambda: "status",
    )
    bot.api_calls = []

    async def _api(method, http_timeout=35.0, **payload):
        bot.api_calls.append((method, payload))
        return {"ok": True}

    bot._api = _api
    return bot


def _press(data: str) -> dict:
    return {
        "id": "cb1", "data": data,
        "message": {"chat": {"id": 1}, "message_id": 7},
    }


class TestSettingsMenu:
    @pytest.mark.asyncio
    async def test_settings_command_shows_the_hub(self, tmp_path):
        bot = _bot(tmp_path)
        i18n.set_language("ru")
        await bot._handle_command("/settings")
        method, payload = bot.api_calls[-1]
        assert method == "sendMessage"
        assert "⚙️ <b>Настройки</b>" in payload["text"]
        assert "🌐 Язык: Русский" in payload["text"]
        buttons = [
            b["callback_data"]
            for row in payload["reply_markup"]["inline_keyboard"] for b in row
        ]
        assert buttons == ["st_lang", "st_pairs", "st_notify", "st_pause"]

    @pytest.mark.asyncio
    async def test_language_switch_persists_and_reregisters_the_menu(self, tmp_path):
        bot = _bot(tmp_path)
        i18n.set_language("ru")
        await bot._handle_callback(_press("st_lang_en"))
        assert i18n.get_language() == "en"
        assert bot.state.language == "en"
        assert WatcherState(Database(str(tmp_path / "smc.db"))).language == "en"
        methods = [m for m, _ in bot.api_calls]
        assert "setMyCommands" in methods  # the slash menu follows the language
        commands = next(p for m, p in bot.api_calls if m == "setMyCommands")["commands"]
        assert [c["command"] for c in commands] == [
            "plan", "journal", "news", "settings", "help",
        ]
        assert commands[3]["description"] == "Language, pairs, alert level, pause"
        edit = next(p for m, p in bot.api_calls if m == "editMessageText")
        assert edit["text"] == "🌐 <b>Language</b>"
        marks = [b["text"] for b in edit["reply_markup"]["inline_keyboard"][0]]
        assert marks == ["🇷🇺 Русский", "🇬🇧 English ✅"]
        answer = next(p for m, p in bot.api_calls if m == "answerCallbackQuery")
        assert answer["text"] == "Language: English"

    @pytest.mark.asyncio
    async def test_language_switch_back_to_russian(self, tmp_path):
        bot = _bot(tmp_path)
        i18n.set_language("en")
        await bot._handle_callback(_press("st_lang_ru"))
        assert i18n.get_language() == "ru" and bot.state.language == "ru"
        commands = next(p for m, p in bot.api_calls if m == "setMyCommands")["commands"]
        assert commands[3]["description"] == "Язык, пары, уровень алертов, пауза"

    @pytest.mark.asyncio
    async def test_unknown_language_is_refused(self, tmp_path):
        bot = _bot(tmp_path)
        i18n.set_language("en")
        await bot._handle_callback(_press("st_lang_de"))
        assert i18n.get_language() == "en" and bot.state.language is None
        answer = next(p for m, p in bot.api_calls if m == "answerCallbackQuery")
        assert answer["text"] == "Unknown language"

    @pytest.mark.asyncio
    async def test_pairs_page_toggles_and_redraws(self, tmp_path):
        bot = _bot(tmp_path)
        before = list(bot.state.pairs)
        await bot._handle_callback(_press("st_pair_ETHUSD"))
        assert ("ETHUSD" in bot.state.pairs) != ("ETHUSD" in before)
        edit = next(p for m, p in bot.api_calls if m == "editMessageText")
        assert edit["text"].startswith("📊 <b>Pairs</b>")
        labels = [
            row[0]["text"] for row in edit["reply_markup"]["inline_keyboard"]
        ]
        assert labels[-1] == "« Back"
        assert any(
            label.endswith("ETHUSD") and label.startswith("☐" if "ETHUSD" in before else "✅")
            for label in labels
        )

    @pytest.mark.asyncio
    async def test_notify_level_buttons(self, tmp_path):
        bot = _bot(tmp_path)
        await bot._handle_callback(_press("st_notify_star"))
        assert bot.state.notify_level == "star"
        answer = next(p for m, p in bot.api_calls if m == "answerCallbackQuery")
        assert answer["text"] == "Setup alerts: ⭐ only"
        edit = next(p for m, p in bot.api_calls if m == "editMessageText")
        labels = [row[0]["text"] for row in edit["reply_markup"]["inline_keyboard"]]
        assert labels[:3] == ["all setups", "✅ ⭐ only", "no setup alerts"]
        bot.api_calls.clear()
        await bot._handle_callback(_press("st_notify_loud"))
        assert bot.state.notify_level == "star"
        answer = next(p for m, p in bot.api_calls if m == "answerCallbackQuery")
        assert answer["text"] == "Unknown alert level"

    @pytest.mark.asyncio
    async def test_pause_and_resume_from_the_hub(self, tmp_path):
        bot = _bot(tmp_path)
        await bot._handle_callback(_press("st_pause"))
        assert bot.state.paused is True
        edit = next(p for m, p in bot.api_calls if m == "editMessageText")
        assert "⏸ Status: paused" in edit["text"]
        assert edit["reply_markup"]["inline_keyboard"][1][1]["callback_data"] == "st_resume"
        bot.api_calls.clear()
        await bot._handle_callback(_press("st_resume"))
        assert bot.state.paused is False
        edit = next(p for m, p in bot.api_calls if m == "editMessageText")
        assert "▶️ Status: active" in edit["text"]

    @pytest.mark.asyncio
    async def test_back_returns_home(self, tmp_path):
        bot = _bot(tmp_path)
        await bot._handle_callback(_press("st_home"))
        edit = next(p for m, p in bot.api_calls if m == "editMessageText")
        assert edit["text"].startswith("⚙️ <b>Settings</b>")

    @pytest.mark.asyncio
    async def test_typed_legacy_commands_still_answer(self, tmp_path):
        bot = _bot(tmp_path)
        await bot._handle_command("/pause")
        assert bot.state.paused is True
        await bot._handle_command("/resume")
        assert bot.state.paused is False
        await bot._handle_command("/pairs")
        method, payload = bot.api_calls[-1]
        assert method == "sendMessage" and "reply_markup" in payload

    @pytest.mark.asyncio
    async def test_slash_menu_has_no_pairs_pause_resume(self, tmp_path):
        bot = _bot(tmp_path)
        await bot._setup_bot_profile()
        commands = next(p for m, p in bot.api_calls if m == "setMyCommands")["commands"]
        names = [c["command"] for c in commands]
        assert "settings" in names
        assert not {"pairs", "pause", "resume"} & set(names)


class TestWatcherAppliesStoredLanguage:
    def test_stored_choice_wins_at_startup(self, tmp_path, monkeypatch):
        import smc_watcher

        db_file = tmp_path / "smc.db"
        db = Database(str(db_file))
        WatcherState(db).set_language("en")
        db.close() if hasattr(db, "close") else None
        monkeypatch.setattr(smc_watcher, "DB_FILE", str(db_file))
        monkeypatch.setattr(smc_watcher.settings.telegram, "bot_token", "123:dummy")
        monkeypatch.setattr(smc_watcher.settings.telegram, "chat_id", "1")
        i18n.set_language("ru")
        smc_watcher.Watcher()
        assert i18n.get_language() == "en"

    def test_no_choice_keeps_the_process_default(self, tmp_path, monkeypatch):
        import smc_watcher

        monkeypatch.setattr(smc_watcher, "DB_FILE", str(tmp_path / "smc.db"))
        monkeypatch.setattr(smc_watcher.settings.telegram, "bot_token", "123:dummy")
        monkeypatch.setattr(smc_watcher.settings.telegram, "chat_id", "1")
        i18n.set_language("ru")
        smc_watcher.Watcher()
        assert i18n.get_language() == "ru"
