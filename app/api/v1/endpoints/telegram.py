"""Telegram webhook API endpoints."""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import TelegramError, ValidationError
from app.database.connection import get_database
from app.models.telegram import TelegramUpdate
from app.models.user import UserCreate, UserPreferences
from app.services.journal_service import JournalService
from app.services.telegram_service import TelegramService
from app.services.user_service import UserService

router = APIRouter()
logger = structlog.get_logger(__name__)

journal_service = JournalService()


def _is_journal_owner(telegram_id: Optional[int]) -> bool:
    """Whether the given user may use the private trade journal.

    When TELEGRAM_OWNER_ID is unset, the journal is open to everyone.
    """
    owner_id = settings.telegram.owner_id
    if owner_id is None:
        return True
    return telegram_id == owner_id


def get_telegram_service() -> TelegramService:
    """Get telegram service instance."""
    return TelegramService()


def get_user_service() -> UserService:
    """Get user service instance."""
    return UserService()


@router.post("/webhook")
async def telegram_webhook(
    update: TelegramUpdate,
    request: Request,
    telegram_service: TelegramService = Depends(get_telegram_service),
    user_service: UserService = Depends(get_user_service),
    db: AsyncSession = Depends(get_database),
    x_telegram_secret_token: str = Header(None),
):
    """Handle Telegram webhook updates."""
    try:
        # Validate webhook secret if configured
        if settings.telegram.webhook_secret:
            if not telegram_service.validate_webhook_secret(x_telegram_secret_token):
                logger.warning("Invalid webhook secret token")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid secret token",
                )

        logger.info("Received Telegram update", update_id=update.update_id)

        # Process message
        if update.message:
            await _process_message(update.message, telegram_service, user_service, db)

        # Process callback query
        elif update.callback_query:
            await _process_callback_query(
                update.callback_query, telegram_service, user_service, db
            )

        return {"status": "success", "message": "Update processed successfully"}

    except ValidationError as e:
        logger.error("Validation error in webhook", error=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except TelegramError as e:
        logger.error("Telegram error in webhook", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
    except Exception as e:
        logger.error("Unexpected error in webhook", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


async def _process_message(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Process incoming message."""
    try:
        # A screenshot of MetaTrader history -> parse into the trade journal.
        if message.photo:
            await _handle_journal_screenshot(message, telegram_service, db)
            return

        # Get or create user
        user = await _get_or_create_user(message.from_user, user_service, db)

        # Update last active
        await user_service.update_last_active(db, user.telegram_id)

        # Process command
        if message.text and message.text.startswith("/"):
            await _process_command(message, telegram_service, user_service, db)
        else:
            # Handle regular message
            await telegram_service.send_message(
                message.chat.id,
                "Hello! I'm your Forex News Bot. Use /help to see available commands.",
            )

    except Exception as e:
        logger.error("Failed to process message", error=str(e), exc_info=True)
        await telegram_service.send_message(
            message.chat.id,
            "Sorry, I encountered an error processing your message. Please try again.",
        )


async def _process_callback_query(
    callback_query,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Process callback query."""
    try:
        # Answer callback query
        await telegram_service.answer_callback_query(callback_query.id)

        # Process callback data
        if callback_query.data:
            await _process_callback_data(
                callback_query, telegram_service, user_service, db
            )

    except Exception as e:
        logger.error("Failed to process callback query", error=str(e), exc_info=True)


async def _process_command(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Process bot commands."""
    command = message.text.split()[0].lower()

    if command == "/start":
        await telegram_service.send_message(
            message.chat.id,
            "Welcome to Forex News Bot! 🚀\n\n"
            "I'll help you stay updated with the latest forex news and market analysis.\n\n"
            "Available commands:\n"
            "/help - Show this help message\n"
            "/settings - Configure your preferences\n"
            "/news - Get latest forex news\n"
            "/currencies - Manage your currency preferences\n"
            "/impact - Set impact level preferences\n"
            "/digest - Configure daily digest\n"
            "/charts - Enable/disable charts\n"
            "/status - Check your current settings\n"
            "/journal - 📓 Дневник сделок (пришли скриншот истории MT4)",
        )

    elif command == "/help":
        await telegram_service.send_message(
            message.chat.id,
            "📚 Forex News Bot Commands:\n\n"
            "/start - Welcome message\n"
            "/help - Show this help\n"
            "/settings - Configure preferences\n"
            "/news - Get latest news\n"
            "/currencies - Currency preferences\n"
            "/impact - Impact level settings\n"
            "/digest - Daily digest settings\n"
            "/charts - Chart preferences\n"
            "/status - Current settings\n"
            "/journal - 📓 Дневник сделок (скриншот истории MT4)\n"
            "/support - Get support",
        )

    elif command == "/settings":
        await _show_settings_menu(message, telegram_service, user_service, db)

    elif command == "/news":
        await _get_latest_news(message, telegram_service, user_service, db)

    elif command == "/currencies":
        await _show_currency_menu(message, telegram_service, user_service, db)

    elif command == "/impact":
        await _show_impact_menu(message, telegram_service, user_service, db)

    elif command == "/digest":
        await _show_digest_menu(message, telegram_service, user_service, db)

    elif command == "/charts":
        await _show_chart_menu(message, telegram_service, user_service, db)

    elif command == "/status":
        await _show_status(message, telegram_service, user_service, db)

    elif command == "/journal":
        await _show_journal(message, telegram_service, db)

    elif command == "/support":
        await telegram_service.send_message(
            message.chat.id,
            "🆘 Support\n\n"
            "If you need help or have questions:\n\n"
            "📧 Email: support@forexbot.com\n"
            "💬 Telegram: @forexbot_support\n"
            "🌐 Website: https://forexbot.com\n\n"
            "We're here to help! 😊",
        )

    else:
        await telegram_service.send_message(
            message.chat.id, "Unknown command. Use /help to see available commands."
        )


async def _get_or_create_user(
    telegram_user, user_service: UserService, db: AsyncSession
):
    """Get or create user from Telegram user data."""
    try:
        # Try to get existing user
        user = await user_service.get_by_telegram_id(db, telegram_user.id)

        if user:
            return user

        # Create new user
        user_data = UserCreate(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
            is_bot=telegram_user.is_bot,
            is_premium=telegram_user.is_premium or False,
            preferences=UserPreferences(),
        )

        return await user_service.create_user(db, user_data)

    except Exception as e:
        logger.error(
            "Failed to get or create user",
            telegram_id=telegram_user.id,
            error=str(e),
            exc_info=True,
        )
        raise


async def _show_settings_menu(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Show settings menu."""
    keyboard = {
        "inline_keyboard": [
            [{"text": "💰 Currencies", "callback_data": "settings_currencies"}],
            [{"text": "⚡ Impact Levels", "callback_data": "settings_impact"}],
            [{"text": "📧 Notifications", "callback_data": "settings_notifications"}],
            [{"text": "📊 Charts", "callback_data": "settings_charts"}],
            [{"text": "🕐 Daily Digest", "callback_data": "settings_digest"}],
            [{"text": "🔙 Back", "callback_data": "back_to_main"}],
        ]
    }

    await telegram_service.send_message(
        message.chat.id,
        "⚙️ Settings\n\nChoose what you'd like to configure:",
        reply_markup=keyboard,
    )


async def _get_latest_news(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Get latest forex news."""
    await telegram_service.send_message(
        message.chat.id,
        "📰 Latest Forex News\n\n"
        "🔴 High Impact:\n"
        "• USD Non-Farm Payrolls - 14:30 UTC\n"
        "• EUR ECB Interest Rate Decision - 14:45 UTC\n\n"
        "🟡 Medium Impact:\n"
        "• GBP Bank of England Rate Decision - 13:00 UTC\n"
        "• JPY Bank of Japan Policy Rate - 03:00 UTC\n\n"
        "Use /settings to customize which news you want to receive.",
    )


async def _show_currency_menu(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Show currency selection menu."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "💵 USD", "callback_data": "currency_USD"},
                {"text": "💶 EUR", "callback_data": "currency_EUR"},
            ],
            [
                {"text": "💷 GBP", "callback_data": "currency_GBP"},
                {"text": "💴 JPY", "callback_data": "currency_JPY"},
            ],
            [
                {"text": "💵 AUD", "callback_data": "currency_AUD"},
                {"text": "💵 CAD", "callback_data": "currency_CAD"},
            ],
            [
                {"text": "🥇 Gold", "callback_data": "currency_XAU"},
                {"text": "₿ Bitcoin", "callback_data": "currency_BTC"},
            ],
            [{"text": "🔙 Back", "callback_data": "back_to_settings"}],
        ]
    }

    await telegram_service.send_message(
        message.chat.id,
        "💰 Currency Preferences\n\nSelect currencies you want to follow:",
        reply_markup=keyboard,
    )


async def _show_impact_menu(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Show impact level menu."""
    keyboard = {
        "inline_keyboard": [
            [{"text": "🔴 High Impact", "callback_data": "impact_high"}],
            [{"text": "🟡 Medium Impact", "callback_data": "impact_medium"}],
            [{"text": "🟢 Low Impact", "callback_data": "impact_low"}],
            [{"text": "🔙 Back", "callback_data": "back_to_settings"}],
        ]
    }

    await telegram_service.send_message(
        message.chat.id,
        "⚡ Impact Level Preferences\n\nSelect impact levels you want to follow:",
        reply_markup=keyboard,
    )


async def _show_digest_menu(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Show digest menu."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🕐 08:00", "callback_data": "digest_08:00"},
                {"text": "🕘 09:00", "callback_data": "digest_09:00"},
            ],
            [
                {"text": "🕙 10:00", "callback_data": "digest_10:00"},
                {"text": "🕚 11:00", "callback_data": "digest_11:00"},
            ],
            [
                {"text": "🕛 12:00", "callback_data": "digest_12:00"},
                {"text": "🕐 13:00", "callback_data": "digest_13:00"},
            ],
            [
                {"text": "🕑 14:00", "callback_data": "digest_14:00"},
                {"text": "🕒 15:00", "callback_data": "digest_15:00"},
            ],
            [{"text": "🔙 Back", "callback_data": "back_to_settings"}],
        ]
    }

    await telegram_service.send_message(
        message.chat.id,
        "🕐 Daily Digest Time\n\nSelect when you want to receive your daily digest:",
        reply_markup=keyboard,
    )


async def _show_chart_menu(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Show chart menu."""
    keyboard = {
        "inline_keyboard": [
            [{"text": "📊 Enable Charts", "callback_data": "charts_enable"}],
            [{"text": "🚫 Disable Charts", "callback_data": "charts_disable"}],
            [{"text": "📈 Single Chart", "callback_data": "chart_type_single"}],
            [{"text": "📊 Multi Chart", "callback_data": "chart_type_multi"}],
            [{"text": "🔙 Back", "callback_data": "back_to_settings"}],
        ]
    }

    await telegram_service.send_message(
        message.chat.id,
        "📊 Chart Preferences\n\nConfigure your chart settings:",
        reply_markup=keyboard,
    )


async def _show_status(
    message,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Show user status."""
    try:
        user = await user_service.get_by_telegram_id(db, message.from_user.id)

        if not user:
            await telegram_service.send_message(message.chat.id, "User not found.")
            return

        status_text = f"👤 Your Current Settings:\n\n"
        status_text += (
            f"💰 Currencies: {', '.join(user.preferred_currencies) or 'None'}\n"
        )
        status_text += f"⚡ Impact Levels: {', '.join(user.impact_levels)}\n"
        status_text += f"📧 Notifications: {'Enabled' if user.notifications_enabled else 'Disabled'}\n"
        status_text += (
            f"📊 Charts: {'Enabled' if user.charts_enabled else 'Disabled'}\n"
        )
        status_text += f"🕐 Digest Time: {user.digest_time}\n"
        status_text += f"🌍 Timezone: {user.timezone}\n"
        status_text += f"📱 Premium: {'Yes' if user.is_premium else 'No'}\n"
        status_text += f"🕐 Last Active: {user.last_active.strftime('%Y-%m-%d %H:%M') if user.last_active else 'Never'}"

        await telegram_service.send_message(message.chat.id, status_text)

    except Exception as e:
        logger.error("Failed to show status", error=str(e), exc_info=True)
        await telegram_service.send_message(
            message.chat.id, "Failed to retrieve status. Please try again."
        )


async def _handle_journal_screenshot(
    message,
    telegram_service: TelegramService,
    db: AsyncSession,
):
    """Parse a MetaTrader history screenshot and offer to save the trades."""
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id

    if not _is_journal_owner(user_id):
        await telegram_service.send_message(
            chat_id, "🔒 Журнал сделок доступен только владельцу бота."
        )
        return

    if not settings.api.openai_api_key:
        await telegram_service.send_message(
            chat_id,
            "⚠️ Распознавание недоступно: не настроен OpenAI API ключ "
            "(API_OPENAI_API_KEY).",
        )
        return

    await telegram_service.send_message(
        chat_id, "🔍 Распознаю сделки со скриншота, подожди пару секунд..."
    )

    try:
        # Largest available photo size is the last one.
        file_id = message.photo[-1].file_id
        image_bytes = await telegram_service.download_file(file_id)
        if not image_bytes:
            await telegram_service.send_message(
                chat_id, "❌ Не удалось скачать изображение. Попробуй ещё раз."
            )
            return

        trades = await journal_service.parse_screenshot(image_bytes)

        if not trades:
            await telegram_service.send_message(
                chat_id, journal_service.format_preview(trades), parse_mode="HTML"
            )
            return

        batch_id = await journal_service.save_pending_batch(db, user_id, trades)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💾 Сохранить", "callback_data": f"jrnl_save_{batch_id}"},
                    {"text": "❌ Отмена", "callback_data": f"jrnl_cancel_{batch_id}"},
                ]
            ]
        }
        await telegram_service.send_message(
            chat_id,
            journal_service.format_preview(trades),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.error("Failed to process journal screenshot", error=str(e), exc_info=True)
        await telegram_service.send_message(
            chat_id,
            "❌ Ошибка при распознавании скриншота. Попробуй прислать более "
            "чёткое изображение.",
        )


async def _show_journal(
    message,
    telegram_service: TelegramService,
    db: AsyncSession,
):
    """Show the /journal statistics."""
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id

    if not _is_journal_owner(user_id):
        await telegram_service.send_message(
            chat_id, "🔒 Журнал сделок доступен только владельцу бота."
        )
        return

    try:
        stats = await journal_service.get_stats(db, user_id)
        await telegram_service.send_message(
            chat_id, journal_service.format_journal(stats), parse_mode="HTML"
        )
    except Exception as e:
        logger.error("Failed to show journal", error=str(e), exc_info=True)
        await telegram_service.send_message(
            chat_id, "❌ Не удалось получить статистику журнала."
        )


async def _handle_journal_callback(
    callback_query,
    telegram_service: TelegramService,
    db: AsyncSession,
):
    """Handle Save / Cancel buttons on a parsed trade batch."""
    data = callback_query.data
    chat_id = callback_query.message.chat.id if callback_query.message else None
    user_id = callback_query.from_user.id

    if not _is_journal_owner(user_id):
        await telegram_service.answer_callback_query(
            callback_query.id, "🔒 Недоступно", show_alert=True
        )
        return

    try:
        if data.startswith("jrnl_save_"):
            batch_id = data.replace("jrnl_save_", "")
            result = await journal_service.confirm_batch(db, user_id, batch_id)
            saved = result["saved"]
            dup = result["duplicates"]
            if saved == 0 and dup == 0:
                text = "⚠️ Нечего сохранять (батч не найден или уже обработан)."
            else:
                text = f"✅ Сохранено сделок: {saved}"
                if dup:
                    text += f"\n♻️ Пропущено дубликатов: {dup}"
            await telegram_service.answer_callback_query(
                callback_query.id, "Готово"
            )
            if chat_id:
                await telegram_service.send_message(chat_id, text, parse_mode="HTML")

        elif data.startswith("jrnl_cancel_"):
            batch_id = data.replace("jrnl_cancel_", "")
            removed = await journal_service.discard_batch(db, user_id, batch_id)
            await telegram_service.answer_callback_query(
                callback_query.id, "Отменено"
            )
            if chat_id:
                await telegram_service.send_message(
                    chat_id,
                    f"❌ Отменено. Сделки не сохранены (удалено: {removed}).",
                )
    except Exception as e:
        logger.error("Failed to handle journal callback", error=str(e), exc_info=True)
        await telegram_service.answer_callback_query(
            callback_query.id, "Ошибка при обработке"
        )


async def _process_callback_data(
    callback_query,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Process callback data."""
    data = callback_query.data

    if data.startswith("jrnl_"):
        await _handle_journal_callback(callback_query, telegram_service, db)
        return

    if data.startswith("currency_"):
        currency = data.replace("currency_", "")
        await _handle_currency_selection(
            callback_query, currency, telegram_service, user_service, db
        )

    elif data.startswith("impact_"):
        impact = data.replace("impact_", "")
        await _handle_impact_selection(
            callback_query, impact, telegram_service, user_service, db
        )

    elif data.startswith("digest_"):
        time_str = data.replace("digest_", "")
        await _handle_digest_selection(
            callback_query, time_str, telegram_service, user_service, db
        )

    elif data.startswith("charts_"):
        action = data.replace("charts_", "")
        await _handle_chart_action(
            callback_query, action, telegram_service, user_service, db
        )

    elif data.startswith("chart_type_"):
        chart_type = data.replace("chart_type_", "")
        await _handle_chart_type_selection(
            callback_query, chart_type, telegram_service, user_service, db
        )

    elif data == "back_to_settings":
        await _show_settings_menu(
            callback_query.message, telegram_service, user_service, db
        )

    elif data == "back_to_main":
        await telegram_service.send_message(
            callback_query.message.chat.id,
            "🏠 Main Menu\n\nUse /help to see available commands.",
        )


async def _handle_currency_selection(
    callback_query,
    currency,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Handle currency selection."""
    try:
        user = await user_service.get_by_telegram_id(db, callback_query.from_user.id)
        if not user:
            return

        # Toggle currency in preferences
        if currency in user.preferred_currencies:
            user.preferred_currencies.remove(currency)
            message = f"❌ {currency} removed from preferences"
        else:
            user.preferred_currencies.append(currency)
            message = f"✅ {currency} added to preferences"

        # Update user preferences
        from app.models.user import UserPreferences

        preferences = UserPreferences(
            preferred_currencies=user.preferred_currencies,
            impact_levels=user.impact_levels,
            analysis_required=user.analysis_required,
            digest_time=user.digest_time,
            timezone=user.timezone,
            notifications_enabled=user.notifications_enabled,
            notification_minutes=user.notification_minutes,
            notification_impact_levels=user.notification_impact_levels,
            charts_enabled=user.charts_enabled,
            chart_type=user.chart_type,
            chart_window_hours=user.chart_window_hours,
        )

        await user_service.update_preferences(db, user.telegram_id, preferences)

        await telegram_service.answer_callback_query(callback_query.id, message)

    except Exception as e:
        logger.error("Failed to handle currency selection", error=str(e), exc_info=True)
        await telegram_service.answer_callback_query(
            callback_query.id, "Failed to update preferences"
        )


async def _handle_impact_selection(
    callback_query,
    impact,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Handle impact level selection."""
    try:
        user = await user_service.get_by_telegram_id(db, callback_query.from_user.id)
        if not user:
            return

        # Toggle impact level in preferences
        if impact in user.impact_levels:
            user.impact_levels.remove(impact)
            message = f"❌ {impact.title()} impact removed from preferences"
        else:
            user.impact_levels.append(impact)
            message = f"✅ {impact.title()} impact added to preferences"

        # Update user preferences
        from app.models.user import UserPreferences

        preferences = UserPreferences(
            preferred_currencies=user.preferred_currencies,
            impact_levels=user.impact_levels,
            analysis_required=user.analysis_required,
            digest_time=user.digest_time,
            timezone=user.timezone,
            notifications_enabled=user.notifications_enabled,
            notification_minutes=user.notification_minutes,
            notification_impact_levels=user.notification_impact_levels,
            charts_enabled=user.charts_enabled,
            chart_type=user.chart_type,
            chart_window_hours=user.chart_window_hours,
        )

        await user_service.update_preferences(db, user.telegram_id, preferences)

        await telegram_service.answer_callback_query(callback_query.id, message)

    except Exception as e:
        logger.error("Failed to handle impact selection", error=str(e), exc_info=True)
        await telegram_service.answer_callback_query(
            callback_query.id, "Failed to update preferences"
        )


async def _handle_digest_selection(
    callback_query,
    time_str,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Handle digest time selection."""
    try:
        user = await user_service.get_by_telegram_id(db, callback_query.from_user.id)
        if not user:
            return

        # Update digest time
        from datetime import time

        digest_time = time.fromisoformat(time_str)

        from app.models.user import UserPreferences

        preferences = UserPreferences(
            preferred_currencies=user.preferred_currencies,
            impact_levels=user.impact_levels,
            analysis_required=user.analysis_required,
            digest_time=digest_time,
            timezone=user.timezone,
            notifications_enabled=user.notifications_enabled,
            notification_minutes=user.notification_minutes,
            notification_impact_levels=user.notification_impact_levels,
            charts_enabled=user.charts_enabled,
            chart_type=user.chart_type,
            chart_window_hours=user.chart_window_hours,
        )

        await user_service.update_preferences(db, user.telegram_id, preferences)

        await telegram_service.answer_callback_query(
            callback_query.id, f"✅ Daily digest set to {time_str}"
        )

    except Exception as e:
        logger.error("Failed to handle digest selection", error=str(e), exc_info=True)
        await telegram_service.answer_callback_query(
            callback_query.id, "Failed to update digest time"
        )


async def _handle_chart_action(
    callback_query,
    action,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Handle chart action."""
    try:
        user = await user_service.get_by_telegram_id(db, callback_query.from_user.id)
        if not user:
            return

        # Update chart settings
        charts_enabled = action == "enable"

        from app.models.user import UserPreferences

        preferences = UserPreferences(
            preferred_currencies=user.preferred_currencies,
            impact_levels=user.impact_levels,
            analysis_required=user.analysis_required,
            digest_time=user.digest_time,
            timezone=user.timezone,
            notifications_enabled=user.notifications_enabled,
            notification_minutes=user.notification_minutes,
            notification_impact_levels=user.notification_impact_levels,
            charts_enabled=charts_enabled,
            chart_type=user.chart_type,
            chart_window_hours=user.chart_window_hours,
        )

        await user_service.update_preferences(db, user.telegram_id, preferences)

        message = f"✅ Charts {'enabled' if charts_enabled else 'disabled'}"
        await telegram_service.answer_callback_query(callback_query.id, message)

    except Exception as e:
        logger.error("Failed to handle chart action", error=str(e), exc_info=True)
        await telegram_service.answer_callback_query(
            callback_query.id, "Failed to update chart settings"
        )


async def _handle_chart_type_selection(
    callback_query,
    chart_type,
    telegram_service: TelegramService,
    user_service: UserService,
    db: AsyncSession,
):
    """Handle chart type selection."""
    try:
        user = await user_service.get_by_telegram_id(db, callback_query.from_user.id)
        if not user:
            return

        # Update chart type
        from app.models.user import UserPreferences

        preferences = UserPreferences(
            preferred_currencies=user.preferred_currencies,
            impact_levels=user.impact_levels,
            analysis_required=user.analysis_required,
            digest_time=user.digest_time,
            timezone=user.timezone,
            notifications_enabled=user.notifications_enabled,
            notification_minutes=user.notification_minutes,
            notification_impact_levels=user.notification_impact_levels,
            charts_enabled=user.charts_enabled,
            chart_type=chart_type,
            chart_window_hours=user.chart_window_hours,
        )

        await user_service.update_preferences(db, user.telegram_id, preferences)

        await telegram_service.answer_callback_query(
            callback_query.id, f"✅ Chart type set to {chart_type}"
        )

    except Exception as e:
        logger.error(
            "Failed to handle chart type selection", error=str(e), exc_info=True
        )
        await telegram_service.answer_callback_query(
            callback_query.id, "Failed to update chart type"
        )


@router.get("/webhook-info")
async def get_webhook_info(
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    """Get Telegram webhook information."""
    try:
        info = await telegram_service.get_webhook_info()
        return info
    except TelegramError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.post("/setup-webhook")
async def setup_webhook(
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    """Setup Telegram webhook."""
    try:
        if not settings.telegram.webhook_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook URL not configured",
            )

        success = await telegram_service.set_webhook(
            settings.telegram.webhook_url, settings.telegram.webhook_secret
        )

        if success:
            return {"status": "success", "message": "Webhook setup completed"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to setup webhook",
            )

    except TelegramError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.delete("/webhook")
async def delete_webhook(
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    """Delete Telegram webhook."""
    try:
        success = await telegram_service.delete_webhook()

        if success:
            return {"status": "success", "message": "Webhook deleted successfully"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete webhook",
            )

    except TelegramError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


@router.post("/test-message")
async def send_test_message(
    chat_id: int,
    message: str,
    telegram_service: TelegramService = Depends(get_telegram_service),
):
    """Send a test message."""
    try:
        success = await telegram_service.send_message(chat_id, message)

        if success:
            return {"status": "success", "message": "Test message sent successfully"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send test message",
            )

    except TelegramError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
