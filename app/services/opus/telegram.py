"""Long-polling command bot of the Opus analyst — its own token, the
owner's chat only. Five commands: /plan (a pair, or buttons), /status,
/journal, /news, /help. The transport discipline is the watcher's
(`smc.telegram_bot`): `_api` never raises, a failed poll backs off, a
plan press is fired-and-forgotten so getUpdates stays free while Opus
thinks."""

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

import httpx
import structlog

from app.services.smc.i18n import t

logger = structlog.get_logger(__name__)


def help_text() -> str:
    return t(
        "<b>Opus analyst</b> — the alternative bot\n\n"
        "Claude Opus reads H4/H1/M5 itself and names the order: a limit, a "
        "market entry, wait (with a zone it wants to see price at) or no "
        "trade. The code only enforces the session window, the red-news "
        "blackout and the minimum RR.\n\n"
        "After a plan I watch the price every 5 minutes: when price reaches "
        "the zone, or a candle closes beyond the invalidation, or a new "
        "session opens, I ask Opus again — a few times a day, never more. "
        "Once you are in, the trade is yours; I only track the outcome for "
        "the journal.\n\n"
        "<b>Commands:</b>\n"
        "/plan — ask Opus about a pair (or all)\n"
        "/status — current plans and open trades\n"
        "/journal — decisions and outcomes, last 30 days\n"
        "/news — today's red news (Forex Factory)\n"
        "/help — this help"
    )


class OpusCommandBot:
    def __init__(
        self,
        bot_token: str,
        owner_chat_id: str,
        pairs: Sequence[str],
        on_plan: Callable[[str], Awaitable[None]],
        status_text: Callable[[], str],
        journal_text: Callable[[], str],
        news_text: Callable[[], str],
    ):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.owner_chat_id = str(owner_chat_id)
        self.pairs = list(pairs)
        self.on_plan = on_plan
        self.status_text = status_text
        self.journal_text = journal_text
        self.news_text = news_text
        self._offset: Optional[int] = None
        self._background_tasks: set = set()

    # ------------------------------------------------------------- transport

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error("Background task failed", task=name, error=str(exc))

        task.add_done_callback(_on_done)
        return task

    async def _api(self, method: str, http_timeout: float = 35.0, **payload) -> Any:
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                response = await client.post(f"{self.base_url}/{method}", json=payload)
                data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Telegram API transport error", method=method, error=str(e))
            return None
        if not data.get("ok"):
            logger.error(
                "Telegram API error", method=method,
                status=data.get("error_code"), description=data.get("description"),
            )
            return None
        return data.get("result")

    async def send(self, text: str, reply_markup: Optional[Dict] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": self.owner_chat_id, "text": text, "parse_mode": "HTML",
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self._api("sendMessage", **payload)

    # ------------------------------------------------------------------ loop

    _BACKOFF_START = 5.0
    _BACKOFF_CAP = 300.0

    async def run(self) -> None:
        try:
            await self._api("deleteWebhook", drop_pending_updates=True)
            await self._setup_bot_profile()
        except Exception as e:
            logger.error("Opus bot startup step failed, continuing", error=str(e))
        logger.info("Opus command bot started (long polling)")
        backoff = self._BACKOFF_START
        while True:
            if await self._poll_once():
                backoff = self._BACKOFF_START
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._BACKOFF_CAP)

    async def _poll_once(self) -> bool:
        updates = await self._api(
            "getUpdates", http_timeout=40.0, offset=self._offset, timeout=30,
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
        await self._api(
            "setMyCommands",
            commands=[
                {"command": "plan", "description": t("Ask Opus about a pair")},
                {"command": "status", "description": t("Current plans and open trades")},
                {"command": "journal", "description": t("Decisions and outcomes")},
                {"command": "news", "description": t("Today's red news (Forex Factory)")},
                {"command": "help", "description": t("What this bot does")},
            ],
        )
        await self._api(
            "setMyShortDescription",
            short_description=t("Claude Opus names the order: limit, market, wait"),
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
            await self._handle_command((message.get("text") or "").strip())
        elif callback:
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            if chat_id != self.owner_chat_id:
                return
            await self._handle_callback(callback)

    def plan_keyboard(self) -> Dict:
        rows = [[{"text": p, "callback_data": f"op_plan:{p}"}] for p in self.pairs]
        rows.append([{"text": t("ALL pairs"), "callback_data": "op_plan:ALL"}])
        return {"inline_keyboard": rows}

    async def _handle_command(self, text: str) -> None:
        if not text.startswith("/"):
            return
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        arg = parts[1].upper() if len(parts) > 1 else ""
        if command in ("/start", "/help"):
            await self.send(help_text())
        elif command == "/plan":
            if not arg:
                await self.send(t("Which pair should Opus read?"), self.plan_keyboard())
                return
            if arg != "ALL" and arg not in self.pairs:
                await self.send(t("Unknown pair: {pair}. Watched: {pairs}",
                                  pair=arg, pairs=", ".join(self.pairs)))
                return
            await self._start_plan(arg)
        elif command == "/status":
            await self.send(self.status_text())
        elif command == "/journal":
            await self.send(self.journal_text())
        elif command == "/news":
            await self.send(self.news_text())
        else:
            await self.send(t("Unknown command. /help lists what I can do."))

    async def _start_plan(self, key: str) -> None:
        targets = self.pairs if key == "ALL" else [key]
        await self.send(t("🧠 Asking Opus about {pairs} — a minute or two…",
                          pairs=", ".join(targets)))
        for pair in targets:
            self._spawn(self.on_plan(pair), f"opus-plan-{pair}")

    async def _handle_callback(self, callback: Dict) -> None:
        answer: Dict[str, Any] = {"callback_query_id": callback["id"]}
        data = str(callback.get("data") or "")
        if data.startswith("op_plan:"):
            key = data.split(":", 1)[1]
            if key == "ALL" or key in self.pairs:
                await self._api("answerCallbackQuery", **answer)
                await self._start_plan(key)
                return
        await self._api("answerCallbackQuery", **answer)
