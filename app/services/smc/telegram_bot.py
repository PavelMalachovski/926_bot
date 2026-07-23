"""Telegram long-polling command bot for the SMC watcher.

The watcher owns the bot token exclusively (the old webhook app is gone), so
getUpdates long polling is safe. Only the owner's chat is served.

Commands:
    /pairs  — toggle watched pairs with inline buttons
    /status — enabled pairs, session, last verdicts
    /check  — run the strategy cycle right now
    /start, /help — description
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional

import httpx
import structlog

from app.services.smc.instruments import INSTRUMENTS
from app.services.smc.state import WatcherState

logger = structlog.get_logger(__name__)

HELP_TEXT = (
    "<b>SMC Watcher</b> — Triple Sync + Imbalance\n\n"
    "I check the selected pairs every 5 minutes during sessions and send:\n"
    "🚨 an urgent alert when a setup is found\n"
    "(no-setup checks go to the logs)\n\n"
    "<b>Commands:</b>\n"
    "/pairs — choose currency pairs\n"
    "/status — current settings and last verdicts\n"
    "/check — run the strategy check right now\n"
    "/plan — pre-market plan for a pair (any time)\n"
    "/stats — signal journal: setups, TP/SL, winrate\n"
    "/journal — trade journal: send an MT4 history screenshot to log trades\n"
    "/news — today's red news (Forex Factory)\n"
    "/help — this help"
)


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
        news_text: Optional[Callable[[], str]] = None,
        on_trade_mark: Optional[Callable[[str, bool], Awaitable[str]]] = None,
        on_plan: Optional[Callable[[str], Awaitable[None]]] = None,
        trade_journal: Optional[Any] = None,
    ):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.owner_chat_id = str(owner_chat_id)
        self.state = state
        self.run_cycle = run_cycle
        self.status_text = status_text
        self.stats_text = stats_text
        self.news_text = news_text
        self.on_trade_mark = on_trade_mark
        self.on_plan = on_plan
        self.trade_journal = trade_journal
        self._offset: Optional[int] = None

    # ------------------------------------------------------------- transport

    async def _api(self, method: str, http_timeout: float = 35.0, **payload) -> Dict:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.post(f"{self.base_url}/{method}", json=payload)
            data = response.json()
            if not data.get("ok"):
                logger.warning("Telegram API error", method=method, response=data)
            return data

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
            file_path = info.get("result", {}).get("file_path")
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

    async def run(self) -> None:
        """Poll Telegram for commands forever."""
        # The old FastAPI bot registered a webhook; getUpdates conflicts with
        # it, so drop it (and any stale backlog) once at startup.
        await self._api("deleteWebhook", drop_pending_updates=True)
        await self._setup_bot_profile()
        logger.info("Telegram command bot started (long polling)")
        while True:
            try:
                # Telegram-side long poll of 30s; HTTP timeout slightly above
                data = await self._api(
                    "getUpdates",
                    http_timeout=40.0,
                    offset=self._offset,
                    timeout=30,
                    allowed_updates=["message", "callback_query"],
                )
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("getUpdates failed, retrying", error=str(e))
                await asyncio.sleep(5)
                continue
            for update in data.get("result", []):
                self._offset = update["update_id"] + 1
                try:
                    await self._handle_update(update)
                except Exception as e:
                    logger.error("Failed to handle update", error=str(e), exc_info=True)

    async def _setup_bot_profile(self) -> None:
        """Register the slash-command menu and profile texts (best effort)."""
        await self._api(
            "setMyCommands",
            commands=[
                {"command": "pairs", "description": "Choose currency pairs"},
                {"command": "check", "description": "Run the strategy check now"},
                {"command": "plan", "description": "Pre-market plan for a pair"},
                {"command": "status", "description": "Settings and last verdicts"},
                {"command": "stats", "description": "Signal journal and winrate"},
                {"command": "journal", "description": "Trade journal from MT4 screenshots"},
                {"command": "news", "description": "Today's red news (Forex Factory)"},
                {"command": "help", "description": "What this bot does"},
            ],
        )
        await self._api(
            "setMyShortDescription",
            short_description="SMC Triple Sync + Imbalance setup alerts",
        )
        await self._api(
            "setMyDescription",
            description=(
                "Watches ETHUSD and forex pairs for Triple Sync + Imbalance "
                "setups (H4 trend → H1 zone → M5 CHoCH + FVG) and sends an "
                "urgent alert with entry/SL/TP when everything lines up. "
                "Trading hours 08-20 Prague."
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
            await self.send(HELP_TEXT)
        elif command == "/pairs":
            await self.send(
                "Select pairs to watch (tap to toggle):",
                reply_markup=self._pairs_keyboard(),
            )
        elif command == "/status":
            await self.send(self.status_text())
        elif command == "/journal":
            if self.trade_journal:
                await self.send(self.trade_journal.stats_text())
            else:
                await self.send("Trade journal is not available.")
        elif command == "/stats":
            if self.stats_text:
                await self.send(self.stats_text())
            else:
                await self.send("Journal is not available.")
        elif command == "/news":
            if self.news_text:
                await self.send(self.news_text())
            else:
                await self.send("News filter is not available.")
        elif command == "/check":
            await self.send("⏳ Checking setups, one moment...")
            summary = await self.run_cycle()
            await self.send(summary)
        elif command == "/plan":
            if not self.on_plan or not self.state.pairs:
                await self.send("No pairs enabled — use /pairs first.")
            else:
                await self.send(
                    "📋 Pre-Market Plan — choose a pair:",
                    reply_markup=self._plan_keyboard(),
                )
        elif command:
            await self.send("Unknown command. /help for the list.")

    async def _handle_screenshot(self, message: Dict) -> None:
        """Parse a MetaTrader history screenshot into the trade journal."""
        if not self.trade_journal:
            await self.send("Trade journal is not available.")
            return
        if not self.trade_journal.api_key:
            await self.send(
                "⚠️ Recognition unavailable: OPENAI_API_KEY is not configured."
            )
            return

        await self.send("🔍 Recognizing trades from the screenshot, one moment...")
        try:
            # Largest available photo size is the last entry.
            file_id = message["photo"][-1]["file_id"]
            image_bytes = await self._download_file(file_id)
            if not image_bytes:
                await self.send("❌ Could not download the image. Please try again.")
                return

            trades = await self.trade_journal.parse_screenshot(image_bytes)
            if not trades:
                await self.send(self.trade_journal.format_preview(trades))
                return

            batch_id = self.trade_journal.save_pending_batch(trades)
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "💾 Save", "callback_data": f"jrnl_save_{batch_id}"},
                        {"text": "❌ Cancel", "callback_data": f"jrnl_cancel_{batch_id}"},
                    ]
                ]
            }
            await self.send(
                self.trade_journal.format_preview(trades), reply_markup=keyboard
            )
        except Exception as e:
            logger.error("Failed to process screenshot", error=str(e), exc_info=True)
            await self.send(
                "❌ Error while recognizing the screenshot. "
                "Please send a clearer image."
            )

    async def _handle_callback(self, callback: Dict) -> None:
        data = callback.get("data", "")
        answer: Dict[str, Any] = {"callback_query_id": callback["id"]}
        if data.startswith(("jrnl_save_", "jrnl_cancel_")) and self.trade_journal:
            await self._handle_journal_callback(data, callback, answer)
            return
        if data.startswith(("take_", "skip_")) and self.on_trade_mark:
            taken = data.startswith("take_")
            signal_id = data.split("_", 1)[1]
            answer["text"] = await self.on_trade_mark(signal_id, taken)
            # replace the buttons with the recorded choice
            message = callback.get("message", {})
            if message:
                chosen = "✅ Taken — tracked in /stats" if taken else "❌ Skipped"
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
        if data.startswith("plan_") and self.on_plan:
            key = data[5:]
            answer["text"] = f"Building {key} plan…"
            await self._api("answerCallbackQuery", **answer)
            await self.on_plan(key)
            return
        if data.startswith("pair_"):
            key = data[5:]
            try:
                enabled = self.state.toggle_pair(key)
                answer["text"] = f"{key}: {'✅ enabled' if enabled else '⛔ disabled'}"
            except KeyError:
                answer["text"] = f"Unknown pair {key}"
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
                    text = "⚠️ Nothing to save (batch not found or already processed)."
                    chosen = "⚠️ Empty"
                else:
                    text = f"✅ Saved trades: {saved}"
                    if dup:
                        text += f"\n♻️ Skipped duplicates: {dup}"
                    chosen = f"💾 Saved ({saved})"
                answer["text"] = "Done"
            else:  # jrnl_cancel_
                batch_id = data[len("jrnl_cancel_"):]
                removed = self.trade_journal.discard_batch(batch_id)
                text = f"❌ Cancelled. Trades were not saved (removed: {removed})."
                chosen = "❌ Cancelled"
                answer["text"] = "Cancelled"

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
            answer["text"] = "Error while processing"
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
        rows.append([{"text": "🌐 All pairs", "callback_data": "plan_ALL"}])
        return {"inline_keyboard": rows}
