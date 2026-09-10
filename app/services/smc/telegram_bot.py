"""Telegram long-polling command bot for the SMC watcher.

The watcher owns the bot token exclusively (the old webhook app is gone), so
getUpdates long polling is safe. Only the owner's chat is served.

Commands (owner decision D27, 2026-09-06 — the minimal set; /settings
replaced /pairs, /pause and /resume on 2026-09-10, owner request):
    /plan   — Strategy audit for a pair on fresh candles + Claude's read
    /journal — trade journal; a photo message parses an MT4 screenshot
    /news   — today's red news
    /settings — ONE menu for every setting: language (ru/en), pairs on/off,
              setup-alert level (all / ⭐ only / none), pause/resume
    /start, /help — description

/pairs, /pause and /resume still answer when typed (muscle memory), but
they are gone from the slash menu and from /help — the ⚙️ menu is the one
place settings live.

All text goes through `i18n.t` (owner request 2026-09-10: Russian by
default, English switchable in /settings).
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx
import structlog

from app.services.smc import i18n
from app.services.smc.i18n import t
from app.services.smc.instruments import INSTRUMENTS
from app.services.smc.state import NOTIFY_LEVELS, WatcherState

logger = structlog.get_logger(__name__)


def help_text() -> str:
    """/start and /help, in the current language."""
    return t(
        "<b>SMC Watcher</b> — Triple Sync + Imbalance\n\n"
        "I check the selected pairs every 5 minutes during sessions and send "
        "exactly two things:\n"
        "📰 the red-news digest at 07:55 Prague\n"
        "🚨 a setup alert when a setup has formed — enter at market — with "
        "Claude's read appended to the card\n\n"
        "<b>Commands:</b>\n"
        "/plan — strategy audit for a pair: pending (limit) entries + Claude's "
        "read, on fresh candles\n"
        "/journal — trade journal: send an MT4 history screenshot to log trades\n"
        "/news — today's red news (Forex Factory)\n"
        "/settings — language, pairs, alert level, pause\n"
        "/help — this help"
    )


# The setup-alert level names (`state.notify_level`) as the owner reads them.
_NOTIFY_LABELS = {
    "all": "all setups",
    "star": "⭐ only",
    "mute": "no setup alerts",
}


class TelegramCommandBot:
    """Minimal getUpdates loop + command routing."""

    def __init__(
        self,
        bot_token: str,
        owner_chat_id: str,
        state: WatcherState,
        run_cycle: Callable[[], Awaitable[str]],
        status_text: Callable[[], str],
        stats_text: Optional[Callable[[], str]] = None,
        ai_stats_text: Optional[Callable[[], str]] = None,
        news_text: Optional[Callable[[], str]] = None,
        pd_text: Optional[Callable[[], str]] = None,
        on_trade_mark: Optional[Callable[[str, bool], Awaitable[str]]] = None,
        on_plan: Optional[Callable[[str], Awaitable[None]]] = None,
        on_setup_analysis: Optional[Callable[[str], Awaitable[None]]] = None,
        on_zone_mute: Optional[
            Callable[[str, Optional[str]], Awaitable[Optional[str]]]
        ] = None,
        trade_journal: Optional[Any] = None,
    ):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.owner_chat_id = str(owner_chat_id)
        self.state = state
        self.run_cycle = run_cycle
        self.status_text = status_text
        self.stats_text = stats_text
        self.ai_stats_text = ai_stats_text
        self.news_text = news_text
        self.pd_text = pd_text
        self.on_trade_mark = on_trade_mark
        self.on_plan = on_plan
        self.on_setup_analysis = on_setup_analysis
        self.on_zone_mute = on_zone_mute
        self.trade_journal = trade_journal
        self._offset: Optional[int] = None
        # Strong references for fire-and-forget plan tasks (see _spawn) — an
        # asyncio.Task with nothing else holding it can be garbage-collected
        # mid-run (documented asyncio footgun), silently killing the task.
        self._background_tasks: set = set()

    # ------------------------------------------------------------- transport

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Fire-and-forget a background coroutine (on_plan/on_setup_analysis).

        `/plan ALL` force-fetches every pair fresh through the 8/min rate
        limiter and renders a chart each — awaiting it inline here used to
        block getUpdates for ~90s, during which even /pause could not take
        effect. The Watcher's own `_get_cycle_lock()` still serializes it
        against a running cycle or another plan build; this only keeps the
        polling loop free while it waits its turn. Exceptions are logged via
        a done-callback since nothing awaits this task directly.

        `_background_tasks` is fetched lazily (not just read from __init__)
        so a bot built via `TelegramCommandBot.__new__` in tests still works
        rather than raising AttributeError — mirrors Watcher._get_cycle_lock.
        """
        tasks = self.__dict__.setdefault("_background_tasks", set())
        task = asyncio.create_task(coro)
        tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(
                    "Background task failed", task=name, error=str(exc),
                    exc_info=exc,
                )

        task.add_done_callback(_on_done)
        return task

    async def _api(self, method: str, http_timeout: float = 35.0, **payload) -> Any:
        """POST to the Bot API; return the `result` payload on success, None
        on any failure. Mirrors `TelegramNotifier._api`'s discipline — a
        transient DNS failure, a non-JSON body from a misbehaving proxy, and
        a parsed `ok: false` (409 second-instance overlap, 401 revoked
        token) are all failures, and none of them may raise: this bot shares
        its process with the scheduler, so an unguarded exception here would
        take alerts down with it."""
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                response = await client.post(f"{self.base_url}/{method}", json=payload)
                data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Telegram API transport error", method=method, error=str(e))
            return None
        if not data.get("ok"):
            logger.error(
                "Telegram API error",
                method=method,
                status=data.get("error_code"),
                description=data.get("description"),
            )
            return None
        return data.get("result")

    async def send(self, text: str, reply_markup: Optional[Dict] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": self.owner_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._api("sendMessage", **payload)

    async def _download_file(self, file_id: str) -> Optional[bytes]:
        """Resolve a file_id via getFile and download its bytes."""
        try:
            info = await self._api("getFile", file_id=file_id)
            file_path = (info or {}).get("file_path")
            if not file_path:
                return None
            url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning("Failed to download file", file_id=file_id, error=str(e))
            return None

    # ------------------------------------------------------------------ loop

    _BACKOFF_START = 5.0
    _BACKOFF_CAP = 300.0

    async def run(self) -> None:
        """Poll Telegram for commands forever."""
        # The old FastAPI bot registered a webhook; getUpdates conflicts with
        # it, so drop it (and any stale backlog) once at startup. `_api`
        # itself never raises (see above), but a flaky boot must not be able
        # to kill this coroutine some other way either: run_forever gathers
        # it alongside the scheduler, so an exception here would take the
        # whole watcher down with it. Log and fall through into polling.
        try:
            await self._api("deleteWebhook", drop_pending_updates=True)
            await self._setup_bot_profile()
        except Exception as e:
            logger.error(
                "Telegram bot startup step failed, continuing",
                error=str(e),
                exc_info=True,
            )
        logger.info("Telegram command bot started (long polling)")
        backoff = self._BACKOFF_START
        while True:
            if await self._poll_once():
                backoff = self._BACKOFF_START
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_CAP)

    async def _poll_once(self) -> bool:
        """One getUpdates round-trip: fetch, dispatch, report success.

        Returns False when the poll was refused or failed — `_api` returns
        None for a transport error, a non-JSON body, or a parsed `ok: false`
        (409 two-instance overlap, 401 revoked token) — so `run()` can back
        off. An empty update list is a normal, successful poll (Telegram's
        own long-poll `timeout=30` already provides the idle wait) and
        returns True with no extra delay.
        """
        updates = await self._api(
            "getUpdates",
            http_timeout=40.0,
            offset=self._offset,
            timeout=30,
            allowed_updates=["message", "callback_query"],
        )
        if updates is None:
            return False
        for update in updates:
            self._offset = update["update_id"] + 1
            try:
                await self._handle_update(update)
            except Exception as e:
                logger.error("Failed to handle update", error=str(e), exc_info=True)
        return True

    async def _setup_bot_profile(self) -> None:
        """Register the slash-command menu and profile texts (best effort),
        in the current language — called at startup and again after every
        language switch so the menu follows the choice."""
        await self._api(
            "setMyCommands",
            commands=[
                {
                    "command": "plan",
                    "description": t("Strategy audit for a pair + Claude's read"),
                },
                {
                    "command": "journal",
                    "description": t("Trade journal from MT4 screenshots"),
                },
                {"command": "news", "description": t("Today's red news (Forex Factory)")},
                {
                    "command": "settings",
                    "description": t("Language, pairs, alert level, pause"),
                },
                {"command": "help", "description": t("What this bot does")},
            ],
        )
        await self._api(
            "setMyShortDescription",
            short_description=t("SMC Triple Sync + Imbalance setup alerts"),
        )
        await self._api(
            "setMyDescription",
            description=t(
                "Watches ETHUSD and forex pairs for Triple Sync + Imbalance "
                "setups (H4 trend → H1 zone → M5 CHoCH + FVG) and sends an "
                "urgent alert with entry/SL/TP when everything lines up. "
                "Trading hours 08:00-18:30 Prague."
            ),
        )

    # -------------------------------------------------------------- handlers

    async def _handle_update(self, update: Dict) -> None:
        message = update.get("message")
        callback = update.get("callback_query")
        if message:
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id != self.owner_chat_id:
                logger.warning("Ignoring message from foreign chat", chat_id=chat_id)
                return
            if message.get("photo"):
                await self._handle_screenshot(message)
                return
            await self._handle_command((message.get("text") or "").strip())
        elif callback:
            chat_id = str(
                callback.get("message", {}).get("chat", {}).get("id", "")
            )
            if chat_id != self.owner_chat_id:
                return
            await self._handle_callback(callback)

    async def _handle_command(self, text: str) -> None:
        command = text.split()[0].lower() if text else ""
        if command in ("/start", "/help"):
            await self.send(help_text())
        elif command == "/settings":
            await self.send(
                self._settings_text(), reply_markup=self._settings_keyboard()
            )
        elif command == "/pairs":
            # Hidden alias since 2026-09-10: the pairs live in /settings.
            await self.send(
                t("Signals per pair — tap to pause (☐) or resume (✅):"),
                reply_markup=self._pairs_keyboard(),
            )
        elif command == "/pause":
            self.state.set_paused(True)
            await self.send(
                t("⏸ <b>Paused</b> — no alerts or messages until you resume."),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": t("▶️ Resume"), "callback_data": "resume"}]
                    ]
                },
            )
        elif command == "/resume":
            self.state.set_paused(False)
            await self.send(t("▶️ <b>Resumed</b> — watching pairs again."))
        elif command == "/journal":
            if self.trade_journal:
                text = self.trade_journal.stats_text()
                if self.ai_stats_text:
                    # 2026-09-10: how often Claude's alert read was right
                    text += "\n\n" + self.ai_stats_text()
                await self.send(text)
            else:
                await self.send(t("Trade journal is not available."))
        elif command == "/news":
            if self.news_text:
                await self.send(self.news_text())
            else:
                await self.send(t("News filter is not available."))
        elif command == "/plan":
            if not self.on_plan or not self.state.pairs:
                await self.send(t("No pairs enabled — turn one on in /settings first."))
            else:
                await self.send(
                    t("🔬 Strategy audit — choose a pair (fresh candles + "
                      "Claude's read):"),
                    reply_markup=self._plan_keyboard(),
                )
        elif command:
            await self.send(t("Unknown command. /help for the list."))

    async def _handle_screenshot(self, message: Dict) -> None:
        """Parse a MetaTrader history screenshot into the trade journal."""
        if not self.trade_journal:
            await self.send(t("Trade journal is not available."))
            return
        if not self.trade_journal.api_key:
            await self.send(
                t("⚠️ Recognition unavailable: ANTHROPIC_API_KEY is not configured.")
            )
            return

        await self.send(t("🔍 Recognizing trades from the screenshot, one moment..."))
        try:
            # Largest available photo size is the last entry.
            file_id = message["photo"][-1]["file_id"]
            image_bytes = await self._download_file(file_id)
            if not image_bytes:
                await self.send(t("❌ Could not download the image. Please try again."))
                return

            trades = await self.trade_journal.parse_screenshot(image_bytes)
            if not trades:
                await self.send(self.trade_journal.format_preview(trades))
                return

            batch_id = self.trade_journal.save_pending_batch(trades)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": t("💾 Save"), "callback_data": f"jrnl_save_{batch_id}"},
                        {"text": t("❌ Cancel"), "callback_data": f"jrnl_cancel_{batch_id}"},
                    ]
                ]
            }
            await self.send(
                self.trade_journal.format_preview(trades), reply_markup=keyboard
            )
        except Exception as e:
            logger.error("Failed to process screenshot", error=str(e), exc_info=True)
            await self.send(t(
                "❌ Error while recognizing the screenshot. "
                "Please send a clearer image."
            ))

    async def _handle_callback(self, callback: Dict) -> None:
        data = callback.get("data", "")
        answer: Dict[str, Any] = {"callback_query_id": callback["id"]}
        if data.startswith(("jrnl_save_", "jrnl_cancel_")) and self.trade_journal:
            await self._handle_journal_callback(data, callback, answer)
            return
        if data.startswith("st_"):
            await self._handle_settings_callback(data, callback, answer)
            return
        if data.startswith(("take_", "skip_")) and self.on_trade_mark:
            taken = data.startswith("take_")
            signal_id = data.split("_", 1)[1]
            answer["text"] = await self.on_trade_mark(signal_id, taken)
            # replace the buttons with the recorded choice
            message = callback.get("message", {})
            if message:
                chosen = t("✅ Taken — tracked in the journal") if taken else t("❌ Skipped")
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup={
                        "inline_keyboard": [[{"text": chosen, "callback_data": "noop"}]]
                    },
                )
            await self._api("answerCallbackQuery", **answer)
            return
        if data == "noop":
            await self._api("answerCallbackQuery", **answer)
            return
        if data == "resume":
            self.state.set_paused(False)
            answer["text"] = t("Resumed")
            message = callback.get("message", {})
            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": t("▶️ Resumed"), "callback_data": "noop"}]
                        ]
                    },
                )
            await self._api("answerCallbackQuery", **answer)
            await self.send(t("▶️ <b>Resumed</b> — watching pairs again."))
            return
        if data.startswith("zmute_") and self.on_zone_mute:
            # "zmute_<PAIR>_<block_id>" — instrument keys carry no
            # underscore, so splitting on the first one recovers both
            # parts. A payload with no block part (an alert message sent
            # before this deploy) yields block_id=None; the hook falls
            # back to the block the press itself falls in.
            parts = data[len("zmute_"):].split("_", 1)
            key = parts[0]
            block_id = parts[1] if len(parts) > 1 else None
            # The hook returns the Prague HH:MM deadline and nothing else,
            # so neither string below has to be parsed back out of a
            # sentence — or None when that block has already ended and
            # nothing was muted.
            until = await self.on_zone_mute(key, block_id)
            message = callback.get("message", {})
            if until is None:
                answer["text"] = t(
                    "{pair}: that alert's session block already ended — "
                    "nothing muted", pair=key,
                )
                if message:
                    await self._api(
                        "editMessageReplyMarkup",
                        chat_id=message["chat"]["id"],
                        message_id=message["message_id"],
                        reply_markup={"inline_keyboard": [[{
                            "text": t("🔕 Block already ended"),
                            "callback_data": "noop",
                        }]]},
                    )
                await self._api("answerCallbackQuery", **answer)
                return
            answer["text"] = t("{pair} zone alerts muted till {hhmm}", pair=key, hhmm=until)
            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup={"inline_keyboard": [[{
                        "text": t("🔕 Muted till {hhmm}", hhmm=until),
                        "callback_data": "noop",
                    }]]},
                )
            await self._api("answerCallbackQuery", **answer)
            return
        if data.startswith("aplan_") and self.on_setup_analysis:
            # D25 (owner decision 2026-09-05): the pair buttons under the
            # 08:05/14:05 summary answer with the Strategy audit — pending
            # (limit) entries, computed on schedule — not the plan text.
            key = data[len("aplan_"):]
            answer["text"] = (
                t("Sending all audits…") if key == "ALL"
                else t("Sending {pair} audit…", pair=key)
            )
            await self._api("answerCallbackQuery", **answer)
            # Fire-and-forget: keeps getUpdates free while the fetch runs
            # (see _spawn). The Watcher's _cycle_lock still serializes it.
            self._spawn(self.on_setup_analysis(key), f"on_setup_analysis:{key}")
            return
        if data.startswith("plan_") and self.on_plan:
            key = data[5:]
            answer["text"] = t("Building {pair} plan…", pair=key)
            await self._api("answerCallbackQuery", **answer)
            self._spawn(self.on_plan(key), f"on_plan:{key}")
            return
        if data.startswith("pair_"):
            key = data[5:]
            answer["text"] = self._toggle_pair_answer(key)
            # refresh the keyboard in place
            message = callback.get("message", {})
            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup=self._pairs_keyboard(),
                )
            await self._api("answerCallbackQuery", **answer)
            return
        await self._api("answerCallbackQuery", **answer)

    async def _handle_journal_callback(
        self, data: str, callback: Dict, answer: Dict[str, Any]
    ) -> None:
        """Save or discard a parsed trade batch."""
        message = callback.get("message", {})
        try:
            if data.startswith("jrnl_save_"):
                batch_id = data[len("jrnl_save_"):]
                result = self.trade_journal.confirm_batch(batch_id)
                saved, dup = result["saved"], result["duplicates"]
                if saved == 0 and dup == 0:
                    text = t("⚠️ Nothing to save (batch not found or already processed).")
                    chosen = t("⚠️ Empty")
                else:
                    text = t("✅ Saved trades: {n}", n=saved)
                    if dup:
                        text += "\n" + t("♻️ Skipped duplicates: {n}", n=dup)
                    chosen = t("💾 Saved ({n})", n=saved)
                answer["text"] = t("Done")
            else:  # jrnl_cancel_
                batch_id = data[len("jrnl_cancel_"):]
                removed = self.trade_journal.discard_batch(batch_id)
                text = t("❌ Cancelled. Trades were not saved (removed: {n}).", n=removed)
                chosen = t("❌ Cancelled")
                answer["text"] = t("Cancelled")

            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": chosen, "callback_data": "noop"}]
                        ]
                    },
                )
            await self._api("answerCallbackQuery", **answer)
            await self.send(text)
        except Exception as e:
            logger.error("Journal callback failed", error=str(e), exc_info=True)
            answer["text"] = t("Error while processing")
            await self._api("answerCallbackQuery", **answer)

    # -------------------------------------------------------------- settings
    #
    # Owner request 2026-09-10: ONE ⚙️ menu holds every setting — language,
    # pairs, the setup-alert level (the retired /notify, back as a button)
    # and pause/resume. Every press edits the same message in place
    # (editMessageText), so the chat never fills up with menu copies.
    # Callback data is prefixed `st_` and never collides with the legacy
    # `pair_*` / `resume` payloads, which still answer for old messages.

    def _settings_text(self) -> str:
        pairs = ", ".join(self.state.pairs) if self.state.pairs else t("none")
        lang = i18n.get_language()
        lines = [
            f"⚙️ <b>{t('Settings')}</b>",
            f"🌐 {t('Language')}: {i18n.LANGUAGE_NAMES[lang]}",
            f"📊 {t('Pairs')}: {pairs}",
            f"🔔 {t('Setup alerts')}: "
            + t(_NOTIFY_LABELS.get(self.state.notify_level, self.state.notify_level)),
            (f"⏸ {t('Status')}: {t('paused')}" if self.state.paused
             else f"▶️ {t('Status')}: {t('active')}"),
        ]
        return "\n".join(lines)

    def _settings_keyboard(self) -> Dict:
        pause_button = (
            {"text": t("▶️ Resume"), "callback_data": "st_resume"}
            if self.state.paused
            else {"text": t("⏸ Pause"), "callback_data": "st_pause"}
        )
        return {"inline_keyboard": [
            [
                {"text": t("🌐 Language"), "callback_data": "st_lang"},
                {"text": t("📊 Pairs"), "callback_data": "st_pairs"},
            ],
            [
                {"text": t("🔔 Alerts"), "callback_data": "st_notify"},
                pause_button,
            ],
        ]}

    def _back_row(self) -> list:
        return [{"text": t("« Back"), "callback_data": "st_home"}]

    def _language_keyboard(self) -> Dict:
        current = i18n.get_language()
        row = [
            {
                "text": f"{i18n.LANGUAGE_FLAGS[code]} {i18n.LANGUAGE_NAMES[code]}"
                + (" ✅" if code == current else ""),
                "callback_data": f"st_lang_{code}",
            }
            for code in i18n.LANGUAGES
        ]
        return {"inline_keyboard": [row, self._back_row()]}

    def _settings_pairs_keyboard(self) -> Dict:
        rows = []
        for key in INSTRUMENTS:
            mark = "✅" if key in self.state.pairs else "☐"
            rows.append([{"text": f"{mark} {key}", "callback_data": f"st_pair_{key}"}])
        rows.append(self._back_row())
        return {"inline_keyboard": rows}

    def _notify_keyboard(self) -> Dict:
        current = self.state.notify_level
        rows = [
            [{
                "text": ("✅ " if level == current else "") + t(_NOTIFY_LABELS[level]),
                "callback_data": f"st_notify_{level}",
            }]
            for level in NOTIFY_LEVELS
        ]
        rows.append(self._back_row())
        return {"inline_keyboard": rows}

    def _toggle_pair_answer(self, key: str) -> str:
        """Toggle a pair and phrase the toast — shared by the /settings
        pairs page and the legacy `pair_*` buttons."""
        try:
            enabled = self.state.toggle_pair(key)
        except KeyError:
            return t("Unknown pair {pair}", pair=key)
        return (
            t("{pair}: ✅ enabled", pair=key) if enabled
            else t("{pair}: ⛔ disabled", pair=key)
        )

    async def _edit_menu(self, message: Dict, text: str, reply_markup: Dict) -> None:
        """Redraw a settings page in place; no message -> nothing to edit."""
        if not message:
            return
        await self._api(
            "editMessageText",
            chat_id=message["chat"]["id"],
            message_id=message["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _handle_settings_callback(
        self, data: str, callback: Dict, answer: Dict[str, Any]
    ) -> None:
        message = callback.get("message", {})
        page = "home"
        if data == "st_lang":
            page = "lang"
        elif data.startswith("st_lang_"):
            code = data[len("st_lang_"):]
            if i18n.normalize_language(code) is None:
                answer["text"] = t("Unknown language")
            else:
                self.state.set_language(code)
                i18n.set_language(code)
                answer["text"] = t("Language: {name}", name=i18n.LANGUAGE_NAMES[code])
                # the slash menu and the bot profile follow the language
                await self._setup_bot_profile()
            page = "lang"
        elif data == "st_pairs":
            page = "pairs"
        elif data.startswith("st_pair_"):
            answer["text"] = self._toggle_pair_answer(data[len("st_pair_"):])
            page = "pairs"
        elif data == "st_notify":
            page = "notify"
        elif data.startswith("st_notify_"):
            level = data[len("st_notify_"):]
            try:
                self.state.set_notify_level(level)
                answer["text"] = t("Setup alerts: {level}", level=t(_NOTIFY_LABELS[level]))
            except (ValueError, KeyError):
                answer["text"] = t("Unknown alert level")
            page = "notify"
        elif data == "st_pause":
            self.state.set_paused(True)
            answer["text"] = t("Paused")
        elif data == "st_resume":
            self.state.set_paused(False)
            answer["text"] = t("Resumed")
        elif data != "st_home":
            await self._api("answerCallbackQuery", **answer)
            return

        if page == "lang":
            await self._edit_menu(
                message, f"🌐 <b>{t('Language')}</b>", self._language_keyboard()
            )
        elif page == "pairs":
            await self._edit_menu(
                message,
                f"📊 <b>{t('Pairs')}</b> — " + t("tap to pause (☐) or resume (✅)"),
                self._settings_pairs_keyboard(),
            )
        elif page == "notify":
            await self._edit_menu(
                message,
                f"🔔 <b>{t('Setup alerts')}</b> — "
                + t("a ⭐ always goes through; regular setups are still "
                    "journal-recorded when not sent"),
                self._notify_keyboard(),
            )
        else:
            await self._edit_menu(
                message, self._settings_text(), self._settings_keyboard()
            )
        await self._api("answerCallbackQuery", **answer)

    def _pairs_keyboard(self) -> Dict:
        rows = []
        for key in INSTRUMENTS:
            mark = "✅" if key in self.state.pairs else "☐"
            rows.append([{"text": f"{mark} {key}", "callback_data": f"pair_{key}"}])
        return {"inline_keyboard": rows}

    def _plan_keyboard(self) -> Dict:
        """One button per enabled pair (two per row) + an 'All pairs' button."""
        pairs = list(self.state.pairs)
        rows = [
            [
                {"text": k, "callback_data": f"plan_{k}"}
                for k in pairs[i : i + 2]
            ]
            for i in range(0, len(pairs), 2)
        ]
        rows.append([{"text": t("🌐 All pairs"), "callback_data": "plan_ALL"}])
        return {"inline_keyboard": rows}
