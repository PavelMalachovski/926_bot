"""HTML parsing methods for the scraping service."""

import re
from datetime import datetime
from typing import List, Dict, Any
import structlog

logger = structlog.get_logger(__name__)


class ForexNewsParser:
    """Parser for ForexFactory HTML content."""

    # Centralized impact class mapping (support both '=' and '-')
    IMPACT_CLASS_MAP = {
        'icon--ff-impact=red': 'high',    # Red (equals)
        'icon--ff-impact=ora': 'medium', # Orange (equals)
        'icon--ff-impact=yel': 'low',    # Yellow (equals)
        'icon--ff-impact-red': 'high',   # Red (dash, fallback)
        'icon--ff-impact-ora': 'medium', # Orange (dash, fallback)
        'icon--ff-impact-yel': 'low',    # Yellow (dash, fallback)
    }

    def ensure_all_times_and_sort(self, news_items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Ensure all news items have proper times and sort by currency and time."""
        if not news_items:
            return news_items

        # Find the first valid time if any items are missing times
        first_valid_time = None
        for item in news_items:
            if item["time"] != "N/A" and item["time"].strip():
                first_valid_time = item["time"]
                break

        # If no valid time found, use a default
        if not first_valid_time:
            first_valid_time = "09:00"

        # Ensure all items have a time
        for item in news_items:
            if item["time"] == "N/A" or not item["time"].strip():
                item["time"] = first_valid_time

        # Sort by currency first, then by time
        def sort_key(item):
            currency = item['currency']
            time_str = item['time']

            # Convert time to sortable format
            try:
                # Handle various time formats
                if ":" in time_str:
                    if "am" in time_str.lower() or "pm" in time_str.lower():
                        # Handle 12-hour format
                        time_obj = datetime.strptime(time_str.lower().replace("am", " AM").replace("pm", " PM"), "%I:%M %p")
                    else:
                        # Handle 24-hour format
                        time_obj = datetime.strptime(time_str, "%H:%M")
                    time_minutes = time_obj.hour * 60 + time_obj.minute
                else:
                    # For non-standard time formats, use a default value
                    time_minutes = 0
            except:
                # If time parsing fails, use a default value
                time_minutes = 0

            return (currency, time_minutes)

        # Sort the items
        news_items.sort(key=sort_key)
        return news_items

    def extract_news_data(self, row) -> Dict[str, str]:
        """Extract news data from a table row."""
        logger.debug("EXTRACTING NEWS DATA for row", row=str(row))

        time_elem = row.select_one('.calendar__time')
        time = time_elem.text.strip() if time_elem else "N/A"

        # Robust time to 24h
        time_24 = time
        try:
            if time and time != "N/A":
                t = time.strip().lower().replace(' ', '')
                # Regex for e.g. 3:30am, 12:05pm, 09:00
                m = re.match(r'^(\d{1,2}):(\d{2})(am|pm)?$', t)
                if m:
                    hour, minute, ampm = m.group(1), m.group(2), m.group(3)
                    hour = int(hour)
                    if ampm == 'pm' and hour != 12:
                        hour += 12
                    if ampm == 'am' and hour == 12:
                        hour = 0
                    time_24 = f"{hour:02d}:{minute}"
                else:
                    # Try 24h format fallback
                    time_24 = datetime.strptime(t, "%H:%M").strftime("%H:%M")
        except Exception as e:
            logger.debug("Time parse failed", time=time, error=str(e))
            time_24 = time  # fallback

        actual_elem = row.select_one('.calendar__actual')
        actual = "N/A"
        if actual_elem:
            actual_text = actual_elem.text.strip()
            if actual_text and actual_text != "":
                actual = actual_text

        currency_elem = row.select_one('.calendar__currency')
        currency = currency_elem.text.strip() if currency_elem else "N/A"

        event_elem = row.select_one('.calendar__event-title')
        event = event_elem.text.strip() if event_elem else "N/A"

        forecast_elem = row.select_one('.calendar__forecast')
        forecast = forecast_elem.text.strip() if forecast_elem else "N/A"

        previous_elem = row.select_one('.calendar__previous')
        previous = previous_elem.text.strip() if previous_elem else "N/A"

        # Impact detection (robust)
        impact = "unknown"
        impact_element = (
            row.select_one('.calendar__impact span.icon')
            or row.select_one('.impact span.icon')
        )
        logger.debug("Impact element", element=impact_element, row=str(row))

        if impact_element:
            classes = impact_element.get('class', [])
            if isinstance(classes, str):
                classes = classes.split()
            # Normalize to lowercase for robustness
            classes = [c.lower() for c in classes]
            logger.debug("Classes", classes=classes, mapping_keys=list(self.IMPACT_CLASS_MAP.keys()))

            for class_name, level in self.IMPACT_CLASS_MAP.items():
                # Accept both '-' and '=' in the class name for robustness
                if class_name in classes:
                    logger.debug("IMPACT MATCH", class_name=class_name, level=level)
                    impact = level
                    break

            if impact == "unknown":
                # Try to match by replacing '-' with '=' and vice versa
                for c in classes:
                    c_eq = c.replace('-', '=')
                    c_dash = c.replace('=', '-')
                    if c_eq in self.IMPACT_CLASS_MAP:
                        logger.debug("IMPACT ALT MATCH", class_name=c, as_class=c_eq, level=self.IMPACT_CLASS_MAP[c_eq])
                        impact = self.IMPACT_CLASS_MAP[c_eq]
                        break
                    if c_dash in self.IMPACT_CLASS_MAP:
                        logger.debug("IMPACT ALT MATCH", class_name=c, as_class=c_dash, level=self.IMPACT_CLASS_MAP[c_dash])
                        impact = self.IMPACT_CLASS_MAP[c_dash]
                        break

            # If only 'icon' or empty, treat as tentative/no-impact
            if impact == "unknown":
                if (len(classes) == 1 and classes[0] == 'icon') or not classes:
                    if time_24.lower() in ['tentative', 'all day'] or time.lower() in ['tentative', 'all day']:
                        impact = "tentative"
                    else:
                        impact = "none"
        else:
            # No impact element at all
            if time_24.lower() in ['tentative', 'all day'] or time.lower() in ['tentative', 'all day']:
                impact = "tentative"
            else:
                impact = "none"

        if impact == "unknown":
            logger.warning("Impact unknown for row", row=str(row))

        return {
            "time": self._escape_markdown_v2(time_24),
            "currency": self._escape_markdown_v2(currency),
            "event": self._escape_markdown_v2(event),
            "actual": self._escape_markdown_v2(actual),
            "forecast": self._escape_markdown_v2(forecast),
            "previous": self._escape_markdown_v2(previous),
            "impact": impact,
        }

    def _escape_markdown_v2(self, text: str) -> str:
        """Escape special characters for Telegram MarkdownV2."""
        escape_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in escape_chars:
            text = text.replace(char, f'\\{char}')
        return text
