"""Advanced Telegram service with webhook management, user settings, and Render.com integration."""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, date as dt_date
from typing import Optional, Dict, Any, List, Callable
import pytz

import httpx
import structlog

from app.core.config import settings
from app.core.exceptions import TelegramError
from app.models.telegram import TelegramUpdate
from app.services.database_service import DatabaseService

logger = structlog.get_logger(__name__)


class TelegramBotManager:
    """Manages Telegram bot initialization and webhook setup."""

    def __init__(self, config=None):
        self.config = config or settings
        self.bot_token = self.config.telegram.bot_token
        self.webhook_url = self.config.telegram.webhook_url
        self.webhook_secret = self.config.telegram.webhook_secret
        self.render_hostname = getattr(self.config, 'render_hostname', None)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    async def setup_webhook(self, max_retries: int = 5, initial_delay: int = 10) -> bool:
        """Setup webhook with retry logic and verification."""
        if not self.bot_token:
            logger.warning("Cannot set webhook: Bot token not configured")
            return False
        if not self.render_hostname:
            logger.warning("Cannot set webhook: Render hostname not set")
            return False

        webhook_url = f"https://{self.render_hostname}/webhook"
        logger.info("Attempting to set webhook", url=webhook_url)

        # First, check current webhook status
        try:
            current_webhook = await self.get_webhook_info()
            logger.info("Current webhook info",
                       url=current_webhook.get('url'),
                       pending_updates=current_webhook.get('pending_update_count'),
                       last_error=current_webhook.get('last_error_message'))
        except Exception as e:
            logger.error("Failed to get current webhook info", error=str(e))

        for attempt in range(max_retries):
            try:
                logger.info("Setting webhook attempt", attempt=attempt + 1, max_retries=max_retries, url=webhook_url)

                # Remove existing webhook first
                try:
                    await self.delete_webhook()
                    await asyncio.sleep(2)
                    logger.info("Removed existing webhook")
                except Exception as e:
                    logger.warning("Failed to remove existing webhook", error=str(e))

                # Set new webhook (include secret token if configured)
                result = await self.set_webhook(webhook_url, self.webhook_secret)
                if result:
                    logger.info("Webhook setup returned True")

                    # Verify webhook was set correctly
                    try:
                        await asyncio.sleep(3)  # Wait a bit for Telegram to process
                        new_webhook = await self.get_webhook_info()
                        logger.info("Webhook verification",
                                   url=new_webhook.get('url'),
                                   pending_updates=new_webhook.get('pending_update_count'),
                                   last_error=new_webhook.get('last_error_message'))

                        if new_webhook.get('url') == webhook_url:
                            logger.info("✅ Webhook URL matches expected URL")

                            # Check if there are any pending updates
                            if new_webhook.get('pending_update_count', 0) > 0:
                                logger.info("📥 Found pending updates", count=new_webhook.get('pending_update_count'))

                            # Check for any recent errors
                            if new_webhook.get('last_error_message'):
                                logger.warning("⚠️ Webhook has recent error", error=new_webhook.get('last_error_message'))

                            return True
                        else:
                            logger.warning("❌ Webhook URL mismatch",
                                         expected=webhook_url,
                                         actual=new_webhook.get('url'))
                    except Exception as e:
                        logger.error("Failed to verify webhook", error=str(e))

                    return True
                else:
                    logger.warning("Webhook setup returned False", attempt=attempt + 1)
            except Exception as e:
                logger.error("Failed to set webhook", attempt=attempt + 1, error=str(e))
                if attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.info("Retrying webhook setup", delay=delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("All webhook setup attempts failed")
        return False

    async def check_webhook_status(self) -> Dict[str, Any]:
        """Check the current webhook status and return detailed information."""
        try:
            webhook_info = await self.get_webhook_info()
            return {
                "url": webhook_info.get('url'),
                "has_custom_certificate": webhook_info.get('has_custom_certificate'),
                "pending_update_count": webhook_info.get('pending_update_count'),
                "last_error_date": webhook_info.get('last_error_date'),
                "last_error_message": webhook_info.get('last_error_message'),
                "max_connections": webhook_info.get('max_connections'),
                "allowed_updates": webhook_info.get('allowed_updates'),
                "is_configured": webhook_info.get('url') is not None and webhook_info.get('url') != ""
            }
        except Exception as e:
            return {"error": str(e)}

    async def test_bot_connection(self) -> Dict[str, Any]:
        """Test if the bot can connect to Telegram API."""
        try:
            bot_info = await self.get_me()
            return {
                "success": True,
                "bot_info": {
                    "id": bot_info.get('id'),
                    "username": bot_info.get('username'),
                    "first_name": bot_info.get('first_name'),
                    "can_join_groups": bot_info.get('can_join_groups'),
                    "can_read_all_group_messages": bot_info.get('can_read_all_group_messages'),
                    "supports_inline_queries": bot_info.get('supports_inline_queries')
                }
            }
        except Exception as e:
            return {"error": str(e)}

    def setup_webhook_async(self):
        """Setup webhook asynchronously in a background thread."""
        def delayed_webhook_setup():
            # Reduced delay for faster deployment readiness
            time.sleep(10)
            asyncio.run(self.setup_webhook())
        threading.Thread(target=delayed_webhook_setup, daemon=True).start()
        logger.info("Webhook setup scheduled for 10 seconds after startup")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Send a message to a chat."""
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode
                }

                if reply_markup:
                    payload["reply_markup"] = reply_markup

                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                    timeout=30.0
                )

                if response.status_code == 200:
                    logger.info("Message sent successfully", chat_id=chat_id)
                    return True
                else:
                    logger.error("Failed to send message", chat_id=chat_id, status_code=response.status_code, response=response.text)
                    return False

        except Exception as e:
            logger.error("Failed to send message", chat_id=chat_id, error=str(e), exc_info=True)
            raise TelegramError(f"Failed to send message: {e}")

    async def send_photo(
        self,
        chat_id: int,
        photo: bytes,
        caption: Optional[str] = None
    ) -> bool:
        """Send a photo to a chat."""
        try:
            async with httpx.AsyncClient() as client:
                files = {"photo": ("chart.png", photo, "image/png")}
                data = {"chat_id": chat_id}

                if caption:
                    data["caption"] = caption

                response = await client.post(
                    f"{self.base_url}/sendPhoto",
                    data=data,
                    files=files,
                    timeout=30.0
                )

                if response.status_code == 200:
                    logger.info("Photo sent successfully", chat_id=chat_id)
                    return True
                else:
                    logger.error("Failed to send photo", chat_id=chat_id, status_code=response.status_code, response=response.text)
                    return False

        except Exception as e:
            logger.error("Failed to send photo", chat_id=chat_id, error=str(e), exc_info=True)
            raise TelegramError(f"Failed to send photo: {e}")

    async def send_document(
        self,
        chat_id: int,
        document: bytes,
        filename: str,
        caption: Optional[str] = None
    ) -> bool:
        """Send a document to a chat."""
        try:
            async with httpx.AsyncClient() as client:
                files = {"document": (filename, document, "application/octet-stream")}
                data = {"chat_id": chat_id}

                if caption:
                    data["caption"] = caption

                response = await client.post(
                    f"{self.base_url}/sendDocument",
                    data=data,
                    files=files,
                    timeout=30.0
                )

                if response.status_code == 200:
                    logger.info("Document sent successfully", chat_id=chat_id, filename=filename)
                    return True
                else:
                    logger.error("Failed to send document", chat_id=chat_id, filename=filename, status_code=response.status_code, response=response.text)
                    return False

        except Exception as e:
            logger.error("Failed to send document", chat_id=chat_id, filename=filename, error=str(e), exc_info=True)
            raise TelegramError(f"Failed to send document: {e}")

    async def send_formatted_message(self, chat_id: int, message: str) -> bool:
        """Send a formatted message to a Telegram chat."""
        try:
            # Escape special characters for MarkdownV2
            escaped_message = self.escape_markdown_v2(message)

            # Send message
            return await self.send_message(chat_id, escaped_message, "MarkdownV2")

        except Exception as e:
            logger.error("Failed to send formatted message", chat_id=chat_id, error=str(e))
            raise TelegramError(f"Failed to send formatted message: {e}")

    def escape_markdown_v2(self, text: str) -> str:
        """Escape special characters for MarkdownV2."""
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    async def send_message(self, chat_id: int, message: str, parse_mode: str = "MarkdownV2") -> bool:
        """Send a message to a Telegram chat."""
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": parse_mode
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=data)
                response.raise_for_status()

            logger.info("Message sent successfully", chat_id=chat_id)
            return True

        except httpx.HTTPError as e:
            logger.error("Failed to send message", chat_id=chat_id, error=str(e))
            raise TelegramError(f"Failed to send message: {e}")

    async def get_webhook_info(self) -> Dict[str, Any]:
        """Get webhook information."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.base_url}/getWebhookInfo", timeout=10.0)

                if response.status_code == 200:
                    return response.json()
                else:
                    raise TelegramError(f"Failed to get webhook info: {response.text}")

        except Exception as e:
            logger.error("Failed to get webhook info", error=str(e), exc_info=True)
            raise TelegramError(f"Failed to get webhook info: {e}")

    async def set_webhook(self, url: str, secret_token: Optional[str] = None) -> bool:
        """Set webhook URL."""
        try:
            async with httpx.AsyncClient() as client:
                payload = {"url": url}

                if secret_token:
                    payload["secret_token"] = secret_token

                response = await client.post(
                    f"{self.base_url}/setWebhook",
                    json=payload,
                    timeout=10.0
                )

                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        logger.info("Webhook set successfully", url=url)
                        return True
                    else:
                        logger.error("Failed to set webhook", url=url, result=result)
                        return False
                else:
                    logger.error("Failed to set webhook", url=url, status_code=response.status_code, response=response.text)
                    return False

        except Exception as e:
            logger.error("Failed to set webhook", url=url, error=str(e), exc_info=True)
            raise TelegramError(f"Failed to set webhook: {e}")

    async def delete_webhook(self) -> bool:
        """Delete webhook."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{self.base_url}/deleteWebhook", timeout=10.0)

                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        logger.info("Webhook deleted successfully")
                        return True
                    else:
                        logger.error("Failed to delete webhook", result=result)
                        return False
                else:
                    logger.error("Failed to delete webhook", status_code=response.status_code, response=response.text)
                    return False

        except Exception as e:
            logger.error("Failed to delete webhook", error=str(e), exc_info=True)
            raise TelegramError(f"Failed to delete webhook: {e}")

    async def get_me(self) -> Dict[str, Any]:
        """Get bot information."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.base_url}/getMe", timeout=10.0)

                if response.status_code == 200:
                    return response.json()
                else:
                    raise TelegramError(f"Failed to get bot info: {response.text}")

        except Exception as e:
            logger.error("Failed to get bot info", error=str(e), exc_info=True)
            raise TelegramError(f"Failed to get bot info: {e}")

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False
    ) -> bool:
        """Answer a callback query."""
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "callback_query_id": callback_query_id,
                    "show_alert": show_alert
                }

                if text:
                    payload["text"] = text

                response = await client.post(
                    f"{self.base_url}/answerCallbackQuery",
                    json=payload,
                    timeout=10.0
                )

                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        logger.info("Callback query answered successfully", callback_query_id=callback_query_id)
                        return True
                    else:
                        logger.error("Failed to answer callback query", callback_query_id=callback_query_id, result=result)
                        return False
                else:
                    logger.error("Failed to answer callback query", callback_query_id=callback_query_id, status_code=response.status_code, response=response.text)
                    return False

        except Exception as e:
            logger.error("Failed to answer callback query", callback_query_id=callback_query_id, error=str(e), exc_info=True)
            raise TelegramError(f"Failed to answer callback query: {e}")

    def validate_webhook_secret(self, received_secret: str) -> bool:
        """Validate webhook secret token."""
        if not self.webhook_secret:
            return True  # No secret configured

        return received_secret == self.webhook_secret


class RenderKeepAlive:
    """Manages self-ping functionality to prevent Render.com app from sleeping."""

    def __init__(self, config=None):
        self.config = config or settings
        self.render_hostname = getattr(self.config, 'render_hostname', None)
        self.scheduler = None
        self._setup_scheduler()

    def _setup_scheduler(self):
        """Setup the scheduler for self-ping."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            if not self.render_hostname:
                logger.warning("Render hostname not set, skipping self-ping")
                return
            self.scheduler = BackgroundScheduler()
            self.scheduler.add_job(self._ping_self, 'interval', minutes=5)
            self.scheduler.start()
            logger.info("Started APScheduler for self-ping every 5 minutes")
        except ImportError:
            logger.warning("APScheduler not available, skipping self-ping setup")

    def _ping_self(self):
        """Ping self to keep the app alive."""
        try:
            import requests
            ping_url = f"https://{self.render_hostname}/ping"
            response = requests.get(ping_url, timeout=10)
            if response.status_code == 200:
                logger.info("Ping successful", url=ping_url)
            else:
                logger.error("Ping failed", url=ping_url, status_code=response.status_code)
        except Exception as e:
            logger.error("Ping error", error=str(e))


class TelegramService:
    """Advanced Telegram service with comprehensive functionality."""

    def __init__(self):
        self.bot_manager = TelegramBotManager()
        self.keep_alive = RenderKeepAlive()
        self.db_service = DatabaseService()

    async def initialize(self):
        """Initialize the Telegram service."""
        try:
            # Test bot connection
            connection_test = await self.bot_manager.test_bot_connection()
            if connection_test.get("success"):
                logger.info("Telegram bot connection successful", bot_info=connection_test.get("bot_info"))
            else:
                logger.error("Telegram bot connection failed", error=connection_test.get("error"))

            # Setup webhook if running on Render.com
            if self.bot_manager.render_hostname:
                self.bot_manager.setup_webhook_async()

            logger.info("Telegram service initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize Telegram service", error=str(e), exc_info=True)
            raise

    async def send_message(self, chat_id: int, message: str, parse_mode: str = "MarkdownV2") -> bool:
        """Send a message to a Telegram chat."""
        try:
            return await self.bot_manager.send_message(chat_id, message, parse_mode)
        except Exception as e:
            logger.error("Failed to send message", chat_id=chat_id, error=str(e))
            return False

    async def send_formatted_message(self, chat_id: int, message: str) -> bool:
        """Send a formatted message to a Telegram chat."""
        try:
            return await self.bot_manager.send_formatted_message(chat_id, message)
        except Exception as e:
            logger.error("Failed to send formatted message", chat_id=chat_id, error=str(e))
            return False

    async def setup_webhook(self, max_retries: int = 5, initial_delay: int = 10) -> bool:
        """Setup webhook with retry logic and verification."""
        try:
            return await self.bot_manager.setup_webhook(max_retries, initial_delay)
        except Exception as e:
            logger.error("Failed to setup webhook", error=str(e))
            return False

    async def delete_webhook(self) -> bool:
        """Delete webhook."""
        try:
            return await self.bot_manager.delete_webhook()
        except Exception as e:
            logger.error("Failed to delete webhook", error=str(e))
            return False

    async def get_webhook_info(self) -> Dict[str, Any]:
        """Get webhook information."""
        try:
            return await self.bot_manager.get_webhook_info()
        except Exception as e:
            logger.error("Failed to get webhook info", error=str(e))
            return {"error": str(e)}

    async def send_long_message(self, chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
        """Send a long message by splitting it if necessary."""
        try:
            # Telegram has a 4096 character limit for messages
            max_length = 4096

            if len(text) <= max_length:
                return await self.bot_manager.send_message(chat_id, text, parse_mode)

            # Split message into chunks
            chunks = []
            current_chunk = ""

            for line in text.split('\n'):
                if len(current_chunk + line + '\n') > max_length:
                    if current_chunk:
                        chunks.append(current_chunk.strip())
                        current_chunk = line + '\n'
                    else:
                        # Single line is too long, split it
                        chunks.append(line[:max_length])
                        current_chunk = line[max_length:] + '\n'
                else:
                    current_chunk += line + '\n'

            if current_chunk:
                chunks.append(current_chunk.strip())

            # Send all chunks
            for i, chunk in enumerate(chunks):
                success = await self.bot_manager.send_message(chat_id, chunk, parse_mode)
                if not success:
                    logger.error("Failed to send message chunk", chunk_index=i, total_chunks=len(chunks))
                    return False

                # Small delay between chunks
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.5)

            logger.info("Long message sent successfully", chat_id=chat_id, chunks=len(chunks))
            return True

        except Exception as e:
            logger.error("Failed to send long message", chat_id=chat_id, error=str(e), exc_info=True)
            return False

    async def format_news_message(self, news_items: List[Dict[str, Any]], target_date: datetime, impact_level: str, analysis_required: bool = True, currencies: Optional[List[str]] = None) -> str:
        """Format forex news message for Telegram."""
        date_str = target_date.strftime("%d.%m.%Y")

        # Filter by currencies if specified
        if currencies:
            filtered_items = [item for item in news_items if item.get('currency') in currencies]
            currency_filter_text = f" (Filtered: {', '.join(currencies)})"
        else:
            filtered_items = news_items
            currency_filter_text = ""

        header = f"🗓️ Forex News for {date_str} (CET){currency_filter_text}:\n\n"

        if not filtered_items:
            currency_msg = f" with currencies: {', '.join(currencies)}" if currencies else ""
            return (
                header
                + f"✅ No news found for {date_str} with impact: {impact_level}{currency_msg}\n"
                + "Please check the website for updates."
            )

        # Group by currency and time for group event detection
        grouped = {}
        for item in filtered_items:
            key = (item['currency'], item['time'])
            grouped.setdefault(key, []).append(item)

        message_parts = [header]
        last_currency = None
        for (currency, time), items in sorted(grouped.items()):
            if currency != last_currency:
                if last_currency is not None:
                    message_parts.append("\n" + "="*33 + "\n\n")
                # Currency name with catchy formatting
                message_parts.append(f'💎 <b>{currency}</b> 💎\n')
                last_currency = currency
            # Group event highlight
            if len(items) > 1:
                message_parts.append(f"<b>🚨 GROUP EVENT at {time} ({len(items)} events)</b>\n")
                group_analysis_text = ''
                if analysis_required:
                    candidate = items[0].get('analysis')
                    if candidate:
                        group_analysis_text = str(candidate).replace('\\', '')
                if group_analysis_text:
                    message_parts.append(f"🔍 <b>Group Analysis:</b> {group_analysis_text}\n")
            for idx, item in enumerate(items):
                impact_emoji = {
                    'high': '🔴',
                    'medium': '🟠',
                    'low': '🟡',
                    'tentative': '⏳',
                    'none': '⚪️',
                    'unknown': '❓',
                }.get(item.get('impact', 'unknown'), '❓')
                # Remove unnecessary backslashes from all fields when displaying in HTML
                event = str(item['event']).replace('\\', '') if item['event'] else 'N/A'
                actual = str(item['actual']).replace('\\', '') if item['actual'] else 'N/A'
                forecast = str(item['forecast']).replace('\\', '') if item['forecast'] else 'N/A'
                previous = str(item['previous']).replace('\\', '') if item['previous'] else 'N/A'
                analysis = str(item.get('analysis', '')).replace('\\', '') if item.get('analysis') else ''

                part = (
                    f"⏰ <b>{item['time']}</b> {impact_emoji} <b>Impact:</b> {item.get('impact', 'unknown').capitalize()}\n"
                    f"📰 <b>Event:</b> {event}\n"
                    f"📊 <b>Actual:</b> {actual}\n"
                    f"📈 <b>Forecast:</b> {forecast}\n"
                    f"📉 <b>Previous:</b> {previous}\n"
                )
                if analysis_required and not item.get('group_analysis', False) and analysis:
                    part += f"🔍 <b>Analysis:</b> {analysis}\n"
                # Add new line between events in group, but not after the last one
                if len(items) > 1 and idx < len(items) - 1:
                    part += "\n"
                # Only add main separator if not a group event or not the last in group
                if len(items) == 1 or idx == len(items) - 1:
                    part += "━━━━━━━━━━━━━━━━━━━━\n"
                message_parts.append(part)
        return "".join(message_parts)

    async def process_webhook_update(self, update_data: Dict[str, Any]) -> bool:
        """Process incoming webhook update."""
        try:
            update = TelegramUpdate(**update_data)

            # Handle different types of updates
            if update.message:
                await self._handle_message(update.message)
            elif update.callback_query:
                await self._handle_callback_query(update.callback_query)

            return True
        except Exception as e:
            logger.error("Failed to process webhook update", error=str(e), exc_info=True)
            return False

    async def _handle_message(self, message):
        """Handle incoming message."""
        try:
            if message.text:
                if message.text.startswith('/start') or message.text.startswith('/help'):
                    await self._handle_start_command(message)
                elif message.text.startswith('/settings'):
                    await self._handle_settings_command(message)
                else:
                    await self._handle_text_message(message)
        except Exception as e:
            logger.error("Failed to handle message", error=str(e), exc_info=True)

    async def _handle_callback_query(self, callback_query):
        """Handle callback query."""
        try:
            await self.bot_manager.answer_callback_query(callback_query.id)

            # Handle different callback types
            if callback_query.data.startswith('settings_'):
                await self._handle_settings_callback(callback_query)
            elif callback_query.data.startswith('calendar_'):
                await self._handle_calendar_callback(callback_query)
            # Add more callback handlers as needed

        except Exception as e:
            logger.error("Failed to handle callback query", error=str(e), exc_info=True)

    async def _handle_start_command(self, message):
        """Handle /start command."""
        help_text = self._get_help_text()
        await self.bot_manager.send_message(message.chat.id, help_text, parse_mode="HTML")

    async def _handle_settings_command(self, message):
        """Handle /settings command."""
        try:
            # Get user settings
            user_prefs = await self.db_service.get_user_preferences(message.from_user.id)
            if user_prefs:
                settings_text = self._format_user_settings(user_prefs)
                await self.bot_manager.send_message(message.chat.id, f"⚙️ Your Settings:\n\n{settings_text}", parse_mode="HTML")
            else:
                await self.bot_manager.send_message(message.chat.id, "❌ Settings not available. Please try again later.")
        except Exception as e:
            logger.error("Failed to handle settings command", error=str(e))
            await self.bot_manager.send_message(message.chat.id, "❌ Error retrieving settings.")

    async def _handle_text_message(self, message):
        """Handle regular text message."""
        await self.bot_manager.send_message(message.chat.id, "I don't understand that command. Use /help to see available commands.")

    async def _handle_settings_callback(self, callback_query):
        """Handle settings-related callback."""
        # This would be implemented based on the specific settings functionality
        pass

    async def _handle_calendar_callback(self, callback_query):
        """Handle calendar-related callback."""
        # This would be implemented based on the specific calendar functionality
        pass

    def _get_help_text(self) -> str:
        """Get help text for the bot."""
        return """
🤖 <b>Forex News Bot</b>

<b>Available Commands:</b>
/start - Show this help message
/settings - Configure your preferences
/help - Show this help message

<b>Features:</b>
• Real-time forex news updates
• Customizable currency filters
• Impact level filtering
• Chart generation
• Timezone support

<b>How to use:</b>
1. Use /settings to configure your preferences
2. The bot will automatically send you news updates
3. You can request charts for specific events

For support, contact the bot administrator.
        """

    def _format_user_settings(self, user_prefs: Dict[str, Any]) -> str:
        """Format user settings for display."""
        settings_text = f"""
<b>Currency Preferences:</b> {', '.join(user_prefs.get('preferred_currencies', []))}
<b>Impact Levels:</b> {', '.join(user_prefs.get('impact_levels', []))}
<b>Analysis Required:</b> {'Yes' if user_prefs.get('analysis_required') else 'No'}
<b>Digest Time:</b> {user_prefs.get('digest_time', 'Not set')}
<b>Timezone:</b> {user_prefs.get('timezone', 'Not set')}
<b>Notifications:</b> {'Enabled' if user_prefs.get('notifications_enabled') else 'Disabled'}
<b>Charts:</b> {'Enabled' if user_prefs.get('charts_enabled') else 'Disabled'}
        """
        return settings_text.strip()
