"""Interactive chart visualization service with comprehensive functionality."""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
from io import BytesIO
import structlog

from app.core.config import settings
from app.core.exceptions import VisualizationError
from app.services.database_service import DatabaseService
from app.services.chart_service import chart_service
from app.services.telegram_service import TelegramService

logger = structlog.get_logger(__name__)


class VisualizeHandler:
    """Handler for interactive chart visualization functionality."""

    def __init__(self, db_service: DatabaseService, telegram_service: TelegramService):
        self.db_service = db_service
        self.telegram_service = telegram_service

        # Available currencies for visualization
        self.available_currencies = [
            "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
            "XAU", "BTC", "ETH"
        ]

        # Time windows for visualization
        self.time_windows = [
            ("1h", 1),
            ("2h", 2)
        ]

        # Asymmetric time windows for cross-rate analysis
        self.cross_rate_windows = [
            ("1h before → 1h after", 1, 1),
            ("2h before → 2h after", 2, 2),
        ]

    def get_currency_selection_keyboard(self) -> dict:
        """Generate currency selection keyboard for visualization."""
        try:
            markup = {"inline_keyboard": []}
            row = []

            for currency in self.available_currencies:
                row.append({
                    "text": currency,
                    "callback_data": f"viz_currency_{currency}"
                })

                if len(row) == 3:  # 3 buttons per row
                    markup["inline_keyboard"].append(row)
                    row = []

            # Add remaining buttons if any
            if row:
                markup["inline_keyboard"].append(row)

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Menu", "callback_data": "viz_back"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate currency selection keyboard", error=str(e))
            return {"inline_keyboard": []}

    def get_time_window_keyboard(self, currency: str, chart_type: str = "symmetric") -> dict:
        """Generate time window selection keyboard."""
        try:
            markup = {"inline_keyboard": []}

            if chart_type == "symmetric":
                windows = self.time_windows
            else:  # asymmetric
                windows = self.cross_rate_windows

            for window_info in windows:
                if chart_type == "symmetric":
                    label, hours = window_info
                    callback_data = f"viz_window_{currency}_{hours}_{hours}"
                else:
                    label, before_hours, after_hours = window_info
                    callback_data = f"viz_window_{currency}_{before_hours}_{after_hours}"

                markup["inline_keyboard"].append([{
                    "text": label,
                    "callback_data": callback_data
                }])

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Currencies", "callback_data": "viz_currencies"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate time window keyboard", error=str(e))
            return {"inline_keyboard": []}

    def get_chart_type_keyboard(self, currency: str) -> dict:
        """Generate chart type selection keyboard."""
        try:
            markup = {"inline_keyboard": []}

            chart_types = [
                ("📊 Symmetric", "symmetric"),
                ("📈 Asymmetric", "asymmetric"),
                ("📉 Cross-Rate", "cross_rate")
            ]

            for label, chart_type in chart_types:
                markup["inline_keyboard"].append([{
                    "text": label,
                    "callback_data": f"viz_type_{currency}_{chart_type}"
                }])

            # Add back button
            markup["inline_keyboard"].append([{"text": "🔙 Back to Currencies", "callback_data": "viz_currencies"}])

            return markup

        except Exception as e:
            logger.error("Failed to generate chart type keyboard", error=str(e))
            return {"inline_keyboard": []}

    async def handle_visualize_command(self, user_id: int) -> Tuple[str, dict]:
        """Handle the /visualize command - show currency selection."""
        try:
            message = "📊 **Chart Visualization**\n\nSelect a currency to visualize:"
            keyboard = self.get_currency_selection_keyboard()

            return message, keyboard

        except Exception as e:
            logger.error("Failed to handle visualize command", user_id=user_id, error=str(e))
            return "❌ Error loading visualization options.", {"inline_keyboard": []}

    async def handle_currency_selection(self, user_id: int, currency: str) -> Tuple[str, dict]:
        """Handle currency selection for visualization."""
        try:
            message = f"📊 **{currency} Chart Visualization**\n\nSelect chart type:"
            keyboard = self.get_chart_type_keyboard(currency)

            return message, keyboard

        except Exception as e:
            logger.error("Failed to handle currency selection", user_id=user_id, currency=currency, error=str(e))
            return f"❌ Error selecting {currency}.", {"inline_keyboard": []}

    async def handle_chart_type_selection(self, user_id: int, currency: str, chart_type: str) -> Tuple[str, dict]:
        """Handle chart type selection."""
        try:
            if chart_type == "symmetric":
                message = f"📊 **{currency} Symmetric Chart**\n\nSelect time window:"
                keyboard = self.get_time_window_keyboard(currency, "symmetric")
            elif chart_type == "asymmetric":
                message = f"📈 **{currency} Asymmetric Chart**\n\nSelect time window:"
                keyboard = self.get_time_window_keyboard(currency, "asymmetric")
            elif chart_type == "cross_rate":
                message = f"📉 **{currency} Cross-Rate Analysis**\n\nSelect time window:"
                keyboard = self.get_time_window_keyboard(currency, "asymmetric")
            else:
                return "❌ Invalid chart type.", {"inline_keyboard": []}

            return message, keyboard

        except Exception as e:
            logger.error("Failed to handle chart type selection", user_id=user_id, currency=currency, chart_type=chart_type, error=str(e))
            return f"❌ Error selecting chart type for {currency}.", {"inline_keyboard": []}

    async def handle_time_window_selection(self, user_id: int, currency: str, before_hours: int, after_hours: int) -> Tuple[str, Optional[bytes]]:
        """Handle time window selection and generate chart."""
        try:
            logger.info("Generating visualization chart", user_id=user_id, currency=currency, before_hours=before_hours, after_hours=after_hours)

            # Calculate time range
            now = datetime.now()
            start_time = now - timedelta(hours=before_hours)
            end_time = now + timedelta(hours=after_hours)

            # Generate chart
            chart_image = await self._generate_visualization_chart(currency, start_time, end_time, before_hours, after_hours)

            if chart_image:
                message = f"📊 **{currency} Chart**\n\n⏰ Time Window: {before_hours}h before → {after_hours}h after\n📅 Generated: {now.strftime('%H:%M')}"
                return message, chart_image
            else:
                return f"❌ Failed to generate chart for {currency}.", None

        except Exception as e:
            logger.error("Failed to handle time window selection", user_id=user_id, currency=currency, error=str(e))
            return f"❌ Error generating chart for {currency}.", None

    async def _generate_visualization_chart(self, currency: str, start_time: datetime, end_time: datetime, before_hours: int, after_hours: int) -> Optional[bytes]:
        """Generate visualization chart for the given parameters."""
        try:
            # Create chart request
            from app.models.chart import ChartRequest

            request = ChartRequest(
                currency=currency,
                event_name=f"{currency} Visualization",
                start_time=start_time,
                end_time=end_time,
                chart_type='intraday'
            )

            # Generate chart using chart service
            response = await chart_service.generate_chart(request)

            if response.success and response.chart_image:
                logger.info("Visualization chart generated successfully", currency=currency, size_bytes=len(response.chart_image))
                return response.chart_image
            else:
                logger.error("Failed to generate visualization chart", currency=currency, error=response.error_message)
                return None

        except Exception as e:
            logger.error("Failed to generate visualization chart", currency=currency, error=str(e), exc_info=True)
            return None

    async def handle_callback_query(self, callback_data: str, user_id: int) -> Tuple[bool, str, dict, Optional[bytes]]:
        """Handle visualization-related callback queries."""
        try:
            if callback_data == "viz_back":
                return True, "📊 **Chart Visualization**\n\nSelect a currency to visualize:", self.get_currency_selection_keyboard(), None

            elif callback_data == "viz_currencies":
                return True, "📊 **Chart Visualization**\n\nSelect a currency to visualize:", self.get_currency_selection_keyboard(), None

            elif callback_data.startswith("viz_currency_"):
                currency = callback_data.split("_", 2)[2]
                message, keyboard = await self.handle_currency_selection(user_id, currency)
                return True, message, keyboard, None

            elif callback_data.startswith("viz_type_"):
                parts = callback_data.split("_")
                currency = parts[2]
                chart_type = parts[3]
                message, keyboard = await self.handle_chart_type_selection(user_id, currency, chart_type)
                return True, message, keyboard, None

            elif callback_data.startswith("viz_window_"):
                parts = callback_data.split("_")
                currency = parts[2]
                before_hours = int(parts[3])
                after_hours = int(parts[4])
                message, chart_image = await self.handle_time_window_selection(user_id, currency, before_hours, after_hours)
                return True, message, {}, chart_image

            else:
                return False, "Unknown visualization callback", {}, None

        except Exception as e:
            logger.error("Failed to handle visualization callback", callback_data=callback_data, user_id=user_id, error=str(e))
            return False, f"Error: {str(e)}", {}, None

    async def get_quick_chart(self, user_id: int, currency: str, hours: int = 2) -> Optional[bytes]:
        """Generate a quick chart for a currency."""
        try:
            now = datetime.now()
            start_time = now - timedelta(hours=hours)
            end_time = now + timedelta(hours=hours)

            chart_image = await self._generate_visualization_chart(currency, start_time, end_time, hours, hours)

            if chart_image:
                logger.info("Quick chart generated", user_id=user_id, currency=currency, hours=hours)
                return chart_image
            else:
                logger.error("Failed to generate quick chart", user_id=user_id, currency=currency)
                return None

        except Exception as e:
            logger.error("Failed to generate quick chart", user_id=user_id, currency=currency, error=str(e))
            return None

    async def get_cross_rate_analysis(self, user_id: int, base_currency: str, quote_currency: str, hours: int = 2) -> Optional[bytes]:
        """Generate cross-rate analysis chart."""
        try:
            # Create cross-rate symbol
            cross_rate = f"{base_currency}{quote_currency}"

            now = datetime.now()
            start_time = now - timedelta(hours=hours)
            end_time = now + timedelta(hours=hours)

            chart_image = await self._generate_visualization_chart(cross_rate, start_time, end_time, hours, hours)

            if chart_image:
                logger.info("Cross-rate analysis generated", user_id=user_id, cross_rate=cross_rate, hours=hours)
                return chart_image
            else:
                logger.error("Failed to generate cross-rate analysis", user_id=user_id, cross_rate=cross_rate)
                return None

        except Exception as e:
            logger.error("Failed to generate cross-rate analysis", user_id=user_id, base_currency=base_currency, quote_currency=quote_currency, error=str(e))
            return None

    def get_available_currencies(self) -> List[str]:
        """Get list of available currencies for visualization."""
        return self.available_currencies.copy()

    def get_time_windows(self) -> List[Tuple[str, int]]:
        """Get list of available time windows."""
        return self.time_windows.copy()

    def get_cross_rate_windows(self) -> List[Tuple[str, int, int]]:
        """Get list of available cross-rate time windows."""
        return self.cross_rate_windows.copy()

    def health_check(self) -> Dict[str, Any]:
        """Check the health of the visualization service."""
        try:
            return {
                "status": "healthy",
                "available_currencies": len(self.available_currencies),
                "time_windows": len(self.time_windows),
                "cross_rate_windows": len(self.cross_rate_windows),
                "chart_service_available": True,
                "telegram_service_available": True
            }

        except Exception as e:
            logger.error("Visualization service health check failed", error=str(e))
            return {
                "status": "unhealthy",
                "error": str(e)
            }
