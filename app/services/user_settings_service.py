"""Interactive user settings handler with keyboard generation and callback management."""

import logging
from typing import List, Optional, Tuple
from datetime import datetime, time
import structlog

from app.services.database_service import DatabaseService
from app.core.exceptions import UserSettingsError

logger = structlog.get_logger(__name__)

# Available currencies for selection
AVAILABLE_CURRENCIES = [
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
    "CNY", "INR", "BRL", "RUB", "KRW", "MXN", "SGD", "HKD"
]

# Available impact levels
IMPACT_LEVELS = ["high", "medium", "low"]

# Available notification minutes
NOTIFICATION_MINUTES = [15, 30, 60]

# Available timezones (common ones)
AVAILABLE_TIMEZONES = [
    "Europe/Prague", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Rome",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore", "Australia/Sydney",
    "UTC"
]


class UserSettingsHandler:
    """Handles user settings and preferences management."""

    def __init__(self, db_service: DatabaseService, digest_scheduler=None):
        self.db_service = db_service
        self.digest_scheduler = digest_scheduler

    def get_settings_keyboard(self, user_id: int) -> dict:
        """Generate settings keyboard with current user preferences."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            markup = {"inline_keyboard": []}

            # Current preferences display
            currencies = user.get_currencies_list() if hasattr(user, 'get_currencies_list') else []
            impact_levels = user.get_impact_levels_list() if hasattr(user, 'get_impact_levels_list') else []
            analysis_status = "✅" if getattr(user, 'analysis_required', False) else "❌"
            digest_time = user.digest_time.strftime("%H:%M") if getattr(user, 'digest_time', None) else "08:00"

            # Main settings buttons
            main_buttons = [
                {"text": f"💱 Currencies ({len(currencies)})", "callback_data": "settings_currencies"},
                {"text": f"⚡ Impact ({len(impact_levels)})", "callback_data": "settings_impact"},
                {"text": f"🔍 Analysis {analysis_status}", "callback_data": "settings_analysis"},
                {"text": f"⏰ Digest {digest_time}", "callback_data": "settings_digest"},
                {"text": "🌍 Timezone", "callback_data": "settings_timezone"},
                {"text": "🔔 Notifications", "callback_data": "settings_notifications"},
                {"text": "📊 Charts", "callback_data": "settings_charts"}
            ]

            # Add buttons in rows of 2
            for i in range(0, len(main_buttons), 2):
                row = main_buttons[i:i+2]
                markup["inline_keyboard"].append(row)

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Menu", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate settings keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    def get_currencies_keyboard(self, user_id: int) -> dict:
        """Generate currency selection keyboard."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            current_currencies = user.get_currencies_list() if hasattr(user, 'get_currencies_list') else []

            markup = {"inline_keyboard": []}
            row = []

            for currency in AVAILABLE_CURRENCIES:
                status = "✅" if currency in current_currencies else "⚪"
                row.append({
                    "text": f"{status} {currency}",
                    "callback_data": f"currency_{currency}"
                })

                if len(row) == 3:  # 3 buttons per row
                    markup["inline_keyboard"].append(row)
                    row = []

            # Add remaining buttons if any
            if row:
                markup["inline_keyboard"].append(row)

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Settings", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate currencies keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    def get_impact_keyboard(self, user_id: int) -> dict:
        """Generate impact level selection keyboard."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            current_impact_levels = user.get_impact_levels_list() if hasattr(user, 'get_impact_levels_list') else []

            markup = {"inline_keyboard": []}

            for impact in IMPACT_LEVELS:
                status = "✅" if impact in current_impact_levels else "⚪"
                emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}.get(impact, "⚪")

                markup["inline_keyboard"].append([{
                    "text": f"{status} {emoji} {impact.capitalize()}",
                    "callback_data": f"impact_{impact}"
                }])

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Settings", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate impact keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    def get_digest_time_keyboard(self, user_id: int) -> dict:
        """Generate digest time selection keyboard."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            current_time = user.digest_time if getattr(user, 'digest_time', None) else time(8, 0)

            markup = {"inline_keyboard": []}

            # Hour selection
            hour_buttons = []
            for hour in range(0, 24, 2):  # Every 2 hours
                status = "✅" if hour == current_time.hour else "⚪"
                hour_buttons.append({
                    "text": f"{status} {hour:02d}:00",
                    "callback_data": f"hour_{hour}"
                })

                if len(hour_buttons) == 4:  # 4 buttons per row
                    markup["inline_keyboard"].append(hour_buttons)
                    hour_buttons = []

            # Add remaining hour buttons if any
            if hour_buttons:
                markup["inline_keyboard"].append(hour_buttons)

            # Minute selection (if hour is selected)
            if current_time.hour >= 0:
                minute_buttons = []
                for minute in [0, 15, 30, 45]:
                    status = "✅" if minute == current_time.minute else "⚪"
                    minute_buttons.append({
                        "text": f"{status} :{minute:02d}",
                        "callback_data": f"minute_{minute}"
                    })

                markup["inline_keyboard"].append(minute_buttons)

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Settings", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate digest time keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    def get_timezone_keyboard(self, user_id: int) -> dict:
        """Generate timezone selection keyboard."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            current_timezone = getattr(user, 'timezone', 'Europe/Prague')

            markup = {"inline_keyboard": []}

            for tz in AVAILABLE_TIMEZONES:
                status = "✅" if tz == current_timezone else "⚪"
                tz_display = tz.replace('_', ' ').replace('/', ' / ')

                markup["inline_keyboard"].append([{
                    "text": f"{status} {tz_display}",
                    "callback_data": f"timezone_{tz}"
                }])

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Settings", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate timezone keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    def get_notifications_keyboard(self, user_id: int) -> dict:
        """Generate notification settings keyboard."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            notifications_enabled = getattr(user, 'notifications_enabled', False)
            notification_minutes = getattr(user, 'notification_minutes', 15)
            notification_impact_levels = getattr(user, 'notification_impact_levels', ['high'])

            markup = {"inline_keyboard": []}

            # Enable/disable notifications
            status = "✅" if notifications_enabled else "❌"
            markup["inline_keyboard"].append([{
                "text": f"{status} Notifications",
                "callback_data": "notification_toggle"
            }])

            # Notification minutes
            markup["inline_keyboard"].append([{
                "text": f"⏰ Minutes: {notification_minutes}",
                "callback_data": "notification_minutes"
            }])

            # Notification impact levels
            impact_text = ", ".join(notification_impact_levels) if notification_impact_levels else "None"
            markup["inline_keyboard"].append([{
                "text": f"⚡ Impact: {impact_text}",
                "callback_data": "notification_impact"
            }])

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Settings", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate notifications keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    def get_charts_keyboard(self, user_id: int) -> dict:
        """Generate chart settings keyboard."""
        try:
            user = self.db_service.get_or_create_user(user_id)
            charts_enabled = getattr(user, 'charts_enabled', False)
            chart_type = getattr(user, 'chart_type', 'candlestick')
            chart_window_hours = getattr(user, 'chart_window_hours', 2)

            markup = {"inline_keyboard": []}

            # Enable/disable charts
            status = "✅" if charts_enabled else "❌"
            markup["inline_keyboard"].append([{
                "text": f"{status} Charts",
                "callback_data": "charts_toggle"
            }])

            # Chart type
            markup["inline_keyboard"].append([{
                "text": f"📊 Type: {chart_type}",
                "callback_data": "chart_type"
            }])

            # Chart window
            markup["inline_keyboard"].append([{
                "text": f"⏱️ Window: {chart_window_hours}h",
                "callback_data": "chart_window"
            }])

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Settings", "callback_data": "settings_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate charts keyboard", user_id=user_id, error=str(e))
            return {"inline_keyboard": []}

    async def handle_settings_callback(self, callback_data: str, user_id: int) -> Tuple[bool, str, dict]:
        """Handle settings-related callback queries."""
        try:
            if callback_data == "settings_back":
                return True, "⚙️ Your Settings:", self.get_settings_keyboard(user_id)

            elif callback_data == "settings_currencies":
                return True, "💱 Select Currencies:", self.get_currencies_keyboard(user_id)

            elif callback_data == "settings_impact":
                return True, "⚡ Select Impact Levels:", self.get_impact_keyboard(user_id)

            elif callback_data == "settings_digest":
                return True, "⏰ Select Digest Time:", self.get_digest_time_keyboard(user_id)

            elif callback_data == "settings_timezone":
                return True, "🌍 Select Timezone:", self.get_timezone_keyboard(user_id)

            elif callback_data == "settings_notifications":
                return True, "🔔 Notification Settings:", self.get_notifications_keyboard(user_id)

            elif callback_data == "settings_charts":
                return True, "📊 Chart Settings:", self.get_charts_keyboard(user_id)

            elif callback_data.startswith("currency_"):
                currency = callback_data.split("_", 1)[1]
                await self._toggle_currency(user_id, currency)
                return True, "💱 Select Currencies:", self.get_currencies_keyboard(user_id)

            elif callback_data.startswith("impact_"):
                impact = callback_data.split("_", 1)[1]
                await self._toggle_impact_level(user_id, impact)
                return True, "⚡ Select Impact Levels:", self.get_impact_keyboard(user_id)

            elif callback_data.startswith("hour_"):
                hour = int(callback_data.split("_", 1)[1])
                await self._set_digest_hour(user_id, hour)
                return True, "⏰ Select Digest Time:", self.get_digest_time_keyboard(user_id)

            elif callback_data.startswith("minute_"):
                minute = int(callback_data.split("_", 1)[1])
                await self._set_digest_minute(user_id, minute)
                return True, "⏰ Select Digest Time:", self.get_digest_time_keyboard(user_id)

            elif callback_data.startswith("timezone_"):
                timezone = callback_data.split("_", 1)[1]
                await self._set_timezone(user_id, timezone)
                return True, "🌍 Select Timezone:", self.get_timezone_keyboard(user_id)

            elif callback_data == "notification_toggle":
                await self._toggle_notifications(user_id)
                return True, "🔔 Notification Settings:", self.get_notifications_keyboard(user_id)

            elif callback_data == "charts_toggle":
                await self._toggle_charts(user_id)
                return True, "📊 Chart Settings:", self.get_charts_keyboard(user_id)

            else:
                return False, "Unknown callback", {}

        except Exception as e:
            logger.error("Failed to handle settings callback", callback_data=callback_data, user_id=user_id, error=str(e))
            return False, f"Error: {str(e)}", {}

    async def _toggle_currency(self, user_id: int, currency: str):
        """Toggle currency selection for user."""
        try:
            user = await self.db_service.get_or_create_user(user_id)
            current_currencies = user.get_currencies_list() if hasattr(user, 'get_currencies_list') else []

            if currency in current_currencies:
                current_currencies.remove(currency)
            else:
                current_currencies.append(currency)

            await self.db_service.update_user_preferences(user_id, preferred_currencies=current_currencies)
            logger.info("Currency toggled", user_id=user_id, currency=currency, currencies=current_currencies)

        except Exception as e:
            logger.error("Failed to toggle currency", user_id=user_id, currency=currency, error=str(e))
            raise UserSettingsError(f"Failed to toggle currency: {e}")

    async def _toggle_impact_level(self, user_id: int, impact_level: str):
        """Toggle impact level selection for user."""
        try:
            user = await self.db_service.get_or_create_user(user_id)
            current_impact_levels = user.get_impact_levels_list() if hasattr(user, 'get_impact_levels_list') else []

            if impact_level in current_impact_levels:
                current_impact_levels.remove(impact_level)
            else:
                current_impact_levels.append(impact_level)

            await self.db_service.update_user_preferences(user_id, impact_levels=current_impact_levels)
            logger.info("Impact level toggled", user_id=user_id, impact_level=impact_level, levels=current_impact_levels)

        except Exception as e:
            logger.error("Failed to toggle impact level", user_id=user_id, impact_level=impact_level, error=str(e))
            raise UserSettingsError(f"Failed to toggle impact level: {e}")

    async def _set_digest_hour(self, user_id: int, hour: int):
        """Set digest hour for user."""
        try:
            user = await self.db_service.get_or_create_user(user_id)
            current_time = user.digest_time if getattr(user, 'digest_time', None) else time(8, 0)
            new_time = time(hour, current_time.minute)

            await self.db_service.update_user_preferences(user_id, digest_time=new_time)
            logger.info("Digest hour set", user_id=user_id, hour=hour)

        except Exception as e:
            logger.error("Failed to set digest hour", user_id=user_id, hour=hour, error=str(e))
            raise UserSettingsError(f"Failed to set digest hour: {e}")

    async def _set_digest_minute(self, user_id: int, minute: int):
        """Set digest minute for user."""
        try:
            user = await self.db_service.get_or_create_user(user_id)
            current_time = user.digest_time if getattr(user, 'digest_time', None) else time(8, 0)
            new_time = time(current_time.hour, minute)

            await self.db_service.update_user_preferences(user_id, digest_time=new_time)
            logger.info("Digest minute set", user_id=user_id, minute=minute)

        except Exception as e:
            logger.error("Failed to set digest minute", user_id=user_id, minute=minute, error=str(e))
            raise UserSettingsError(f"Failed to set digest minute: {e}")

    async def _set_timezone(self, user_id: int, timezone: str):
        """Set timezone for user."""
        try:
            await self.db_service.update_user_preferences(user_id, timezone=timezone)
            logger.info("Timezone set", user_id=user_id, timezone=timezone)

        except Exception as e:
            logger.error("Failed to set timezone", user_id=user_id, timezone=timezone, error=str(e))
            raise UserSettingsError(f"Failed to set timezone: {e}")

    async def _toggle_notifications(self, user_id: int):
        """Toggle notifications for user."""
        try:
            user = await self.db_service.get_or_create_user(user_id)
            current_status = getattr(user, 'notifications_enabled', False)
            new_status = not current_status

            await self.db_service.update_user_preferences(user_id, notifications_enabled=new_status)
            logger.info("Notifications toggled", user_id=user_id, enabled=new_status)

        except Exception as e:
            logger.error("Failed to toggle notifications", user_id=user_id, error=str(e))
            raise UserSettingsError(f"Failed to toggle notifications: {e}")

    async def _toggle_charts(self, user_id: int):
        """Toggle charts for user."""
        try:
            user = await self.db_service.get_or_create_user(user_id)
            current_status = getattr(user, 'charts_enabled', False)
            new_status = not current_status

            await self.db_service.update_user_preferences(user_id, charts_enabled=new_status)
            logger.info("Charts toggled", user_id=user_id, enabled=new_status)

        except Exception as e:
            logger.error("Failed to toggle charts", user_id=user_id, error=str(e))
            raise UserSettingsError(f"Failed to toggle charts: {e}")

    def get_user_settings_summary(self, user_id: int) -> str:
        """Get a summary of user settings."""
        try:
            user = self.db_service.get_or_create_user(user_id)

            currencies = user.get_currencies_list() if hasattr(user, 'get_currencies_list') else []
            impact_levels = user.get_impact_levels_list() if hasattr(user, 'get_impact_levels_list') else []
            analysis_required = getattr(user, 'analysis_required', False)
            digest_time = user.digest_time.strftime("%H:%M") if getattr(user, 'digest_time', None) else "08:00"
            timezone = getattr(user, 'timezone', 'Europe/Prague')
            notifications_enabled = getattr(user, 'notifications_enabled', False)
            charts_enabled = getattr(user, 'charts_enabled', False)

            summary = f"""
⚙️ **Your Settings Summary:**

💱 **Currencies:** {', '.join(currencies) if currencies else 'None'}
⚡ **Impact Levels:** {', '.join(impact_levels) if impact_levels else 'None'}
🔍 **Analysis Required:** {'Yes' if analysis_required else 'No'}
⏰ **Digest Time:** {digest_time}
🌍 **Timezone:** {timezone.replace('_', ' ')}
🔔 **Notifications:** {'Enabled' if notifications_enabled else 'Disabled'}
📊 **Charts:** {'Enabled' if charts_enabled else 'Disabled'}
            """

            return summary.strip()

        except Exception as e:
            logger.error("Failed to get user settings summary", user_id=user_id, error=str(e))
            return "❌ Error retrieving settings summary."
