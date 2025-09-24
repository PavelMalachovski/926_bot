"""Telegram utility functions for message formatting and handling."""

import logging
from typing import Optional
import re
import structlog

logger = structlog.get_logger(__name__)


def escape_markdown_v2(text: str) -> str:
    """Escape only Telegram MarkdownV2 special characters in user-supplied text."""
    if not text or text.strip() == "":
        return "N/A"

    # Define the characters that need to be escaped in MarkdownV2
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']

    # Escape backslashes first, then other special characters
    escaped_text = text.replace('\\', '\\\\')
    for char in special_chars:
        escaped_text = escaped_text.replace(char, f'\\{char}')

    return escaped_text


def send_long_message(bot, chat_id, text, parse_mode="MarkdownV2"):
    """Send a long message to Telegram, splitting if needed. Tries MarkdownV2, then HTML, then plain text."""
    max_length = 4096
    try:
        for i in range(0, len(text), max_length):
            bot.send_message(chat_id, text[i:i+max_length], parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"{parse_mode} send failed: {e}. Attempting to fix and retry.")
        # Try HTML fallback
        try:
            html_text = text.replace('**', '<b>').replace('__', '<i>').replace('`', '<code>')
            for i in range(0, len(html_text), max_length):
                bot.send_message(chat_id, html_text[i:i+max_length], parse_mode="HTML")
        except Exception as e2:
            logger.warning(f"HTML send failed: {e2}. Falling back to plain text.")
            for i in range(0, len(text), max_length):
                bot.send_message(chat_id, text[i:i+max_length])


def _fix_markdown_issues(text: str) -> str:
    """Attempt to fix common MarkdownV2 formatting issues."""
    # Remove any existing double escapes
    text = text.replace('\\\\\\\\', '\\\\')

    # Fix common problematic sequences
    problematic_sequences = {
        '\\.\\\\n': '\\.\n',  # Fix escaped periods before newlines
        '\\\\n\\\\n': '\n\n',  # Fix double-escaped newlines
        '\\\\t': '\t',  # Fix escaped tabs
        '\\\\r': '\r',  # Fix escaped carriage returns
    }

    for pattern, replacement in problematic_sequences.items():
        text = re.sub(pattern, replacement, text)

    return text


def format_news_message(
    news_items: list,
    target_date,
    impact_level: str = "high",
    analysis_required: bool = True,
    currencies: Optional[list] = None
) -> str:
    """Format forex news message for Telegram."""
    from datetime import datetime

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


def filter_news_by_impact(news_items, impact_level):
    """Filter news items by impact level."""
    if impact_level == 'all':
        return news_items
    return [item for item in news_items if item.get('impact') == impact_level]


def format_currency_pair(currency_pair: str) -> str:
    """Format currency pair for display."""
    if len(currency_pair) == 6:
        return f"{currency_pair[:3]}/{currency_pair[3:]}"
    return currency_pair


def format_price(price: float, currency: str) -> str:
    """Format price for display based on currency."""
    if 'JPY' in currency:
        return f"{price:.2f}"
    else:
        return f"{price:.4f}"


def format_percentage(percentage: float) -> str:
    """Format percentage for display."""
    return f"{percentage:+.2f}%"


def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text to maximum length."""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."


def clean_html_tags(text: str) -> str:
    """Remove HTML tags from text."""
    import re
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text)


def format_timestamp(timestamp, timezone_str: str = "Europe/Prague") -> str:
    """Format timestamp for display."""
    import pytz
    from datetime import datetime

    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))

    tz = pytz.timezone(timezone_str)
    local_time = timestamp.astimezone(tz)
    return local_time.strftime("%H:%M %Z")


def format_date_range(start_date, end_date, timezone_str: str = "Europe/Prague") -> str:
    """Format date range for display."""
    import pytz
    from datetime import datetime

    tz = pytz.timezone(timezone_str)

    if isinstance(start_date, str):
        start_date = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
    if isinstance(end_date, str):
        end_date = datetime.fromisoformat(end_date.replace('Z', '+00:00'))

    start_local = start_date.astimezone(tz)
    end_local = end_date.astimezone(tz)

    if start_local.date() == end_local.date():
        return f"{start_local.strftime('%d.%m.%Y')} {start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')}"
    else:
        return f"{start_local.strftime('%d.%m.%Y %H:%M')} - {end_local.strftime('%d.%m.%Y %H:%M')}"
