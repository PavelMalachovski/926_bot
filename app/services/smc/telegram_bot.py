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
    "/stats — signal journal: setups, TP/SL, winrate\n"
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
    ):
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.owner_chat_id = str(owner_chat_id)
        self.state = state
        self.run_cycle = run_cycle
        self.status_text = status_text
        self.stats_text = stats_text
        self.news_text = news_text
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

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        """Poll Telegram for commands forever."""
        # The old FastAPI bot registered a webhook; getUpdates conflicts with
        # it, so drop it (and any stale backlog) once at startup.
        await self._api("deleteWebhook", drop_pending_updates=True)
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

    # -------------------------------------------------------------- handlers

    async def _handle_update(self, update: Dict) -> None:
        message = update.get("message")
        callback = update.get("callback_query")
        if message:
            chat_id = str(message.get("chat", {}).get("id", ""))
            if chat_id != self.owner_chat_id:
                logger.warning("Ignoring message from foreign chat", chat_id=chat_id)
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
        elif command:
            await self.send("Unknown command. /help for the list.")

    async def _handle_callback(self, callback: Dict) -> None:
        data = callback.get("data", "")
        answer: Dict[str, Any] = {"callback_query_id": callback["id"]}
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

    def _pairs_keyboard(self) -> Dict:
        rows = []
        for key in INSTRUMENTS:
            mark = "✅" if key in self.state.pairs else "☐"
            rows.append([{"text": f"{mark} {key}", "callback_data": f"pair_{key}"}])
        return {"inline_keyboard": rows}
