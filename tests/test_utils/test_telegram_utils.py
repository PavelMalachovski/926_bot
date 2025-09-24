"""Tests for telegram utilities."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from app.utils.telegram_utils import (
    escape_markdown_v2,
    send_long_message,
    _fix_markdown_issues,
    format_currency_pair,
    format_price,
    format_percentage,
    truncate_text,
    clean_html_tags,
    format_timestamp,
    format_date_range
)


class TestTelegramUtils:
    """Test cases for telegram utilities."""

    def test_escape_markdown_v2_success(self):
        """Test successful MarkdownV2 escaping."""
        # Arrange
        text = "Test message with special chars: _*[]()~`>#+-=|{}.!"

        # Act
        result = escape_markdown_v2(text)

        # Assert
        assert result == "Test message with special chars: \\_\\*\\[\\]\\(\\)\\~\\`\\>\\#\\+\\-\\=\\|\\{\\}\\.\\!"

    def test_escape_markdown_v2_empty_string(self):
        """Test MarkdownV2 escaping with empty string."""
        # Arrange
        text = ""

        # Act
        result = escape_markdown_v2(text)

        # Assert
        assert result == "N/A"

    def test_escape_markdown_v2_none(self):
        """Test MarkdownV2 escaping with None."""
        # Arrange
        text = None

        # Act
        result = escape_markdown_v2(text)

        # Assert
        assert result == "N/A"

    def test_escape_markdown_v2_no_special_chars(self):
        """Test MarkdownV2 escaping with no special characters."""
        # Arrange
        text = "Simple text without special characters"

        # Act
        result = escape_markdown_v2(text)

        # Assert
        assert result == text

    def test_send_long_message_success(self):
        """Test successful long message sending."""
        # Arrange
        mock_bot = AsyncMock()
        chat_id = 123456789
        message = "A" * 5000  # Long message
        mock_bot.send_message.return_value = AsyncMock(message_id=1)

        # Act
        result = send_long_message(mock_bot, chat_id, message)

        # Assert
        assert result is None  # Function doesn't return anything
        # Should split into multiple messages
        assert mock_bot.send_message.call_count > 1

    def test_send_long_message_short_message(self):
        """Test long message sending with short message."""
        # Arrange
        mock_bot = AsyncMock()
        chat_id = 123456789
        message = "Short message"
        mock_bot.send_message.return_value = AsyncMock(message_id=1)

        # Act
        result = send_long_message(mock_bot, chat_id, message)

        # Assert
        assert result is None  # Function doesn't return anything
        # Should send as single message
        assert mock_bot.send_message.call_count == 1

    def test_send_long_message_error(self):
        """Test long message sending with error."""
        # Arrange
        mock_bot = AsyncMock()
        chat_id = 123456789
        message = "Test message"
        mock_bot.send_message.side_effect = Exception("Telegram error")

        # Act - The function catches exceptions and tries fallbacks
        result = send_long_message(mock_bot, chat_id, message)

        # Assert - Function should not raise exception due to fallback handling
        assert result is None

    def test_send_long_message_fallback_success(self):
        """Test long message sending with fallback."""
        # Arrange
        mock_bot = AsyncMock()
        chat_id = 123456789
        message = "A" * 5000  # Long message
        mock_bot.send_message.side_effect = [Exception("Error"), AsyncMock(message_id=1)]

        # Act
        result = send_long_message(mock_bot, chat_id, message)

        # Assert
        assert result is None  # Function doesn't return anything
        # Should retry with fallback
        assert mock_bot.send_message.call_count > 1

    def test_fix_markdown_issues_success(self):
        """Test successful Markdown issue fixing."""
        # Arrange
        text = "**Bold text** and *italic text* and `code`"

        # Act
        result = _fix_markdown_issues(text)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fix_markdown_issues_empty_string(self):
        """Test Markdown issue fixing with empty string."""
        # Arrange
        text = ""

        # Act
        result = _fix_markdown_issues(text)

        # Assert
        assert result == ""

    def test_fix_markdown_issues_none(self):
        """Test Markdown issue fixing with None."""
        # Arrange
        text = None

        # Act & Assert - Function doesn't handle None, so it should raise AttributeError
        with pytest.raises(AttributeError):
            _fix_markdown_issues(text)

    def test_fix_markdown_issues_no_markdown(self):
        """Test Markdown issue fixing with no Markdown."""
        # Arrange
        text = "Simple text without markdown"

        # Act
        result = _fix_markdown_issues(text)

        # Assert
        assert result == text

    def test_format_currency_pair_success(self):
        """Test successful currency pair formatting."""
        # Arrange
        currency_pair = "USDEUR"

        # Act
        result = format_currency_pair(currency_pair)

        # Assert
        assert result == "USD/EUR"

    def test_format_currency_pair_same_currency(self):
        """Test currency pair formatting with same currency."""
        # Arrange
        currency_pair = "USDUSD"

        # Act
        result = format_currency_pair(currency_pair)

        # Assert
        assert result == "USD/USD"

    def test_format_currency_pair_short_string(self):
        """Test currency pair formatting with short string."""
        # Arrange
        currency_pair = "USD"

        # Act
        result = format_currency_pair(currency_pair)

        # Assert
        assert result == "USD"

    def test_format_price_success(self):
        """Test successful price formatting."""
        # Arrange
        price = 1.2345
        currency = "USD"

        # Act
        result = format_price(price, currency)

        # Assert
        assert result == "1.2345"

    def test_format_price_jpy(self):
        """Test price formatting for JPY."""
        # Arrange
        price = 123.45
        currency = "JPY"

        # Act
        result = format_price(price, currency)

        # Assert
        assert result == "123.45"

    def test_format_percentage_success(self):
        """Test successful percentage formatting."""
        # Arrange
        percentage = 2.5

        # Act
        result = format_percentage(percentage)

        # Assert
        assert result == "+2.50%"

    def test_format_percentage_negative(self):
        """Test percentage formatting for negative value."""
        # Arrange
        percentage = -1.25

        # Act
        result = format_percentage(percentage)

        # Assert
        assert result == "-1.25%"

    def test_truncate_text_success(self):
        """Test successful text truncation."""
        # Arrange
        text = "This is a very long text that should be truncated"
        max_length = 20

        # Act
        result = truncate_text(text, max_length)

        # Assert
        assert len(result) <= max_length
        assert result.endswith("...")

    def test_clean_html_tags_success(self):
        """Test successful HTML tag cleaning."""
        # Arrange
        text = "<b>Bold text</b> and <i>italic text</i>"

        # Act
        result = clean_html_tags(text)

        # Assert
        assert "<b>" not in result
        assert "<i>" not in result
        assert "Bold text" in result
        assert "italic text" in result

    def test_format_timestamp_success(self):
        """Test successful timestamp formatting."""
        # Arrange
        from datetime import datetime
        timestamp = datetime(2024, 1, 15, 14, 30, 0)

        # Act
        result = format_timestamp(timestamp)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_date_range_success(self):
        """Test successful date range formatting."""
        # Arrange
        from datetime import datetime
        start_date = datetime(2024, 1, 15, 9, 0, 0)
        end_date = datetime(2024, 1, 15, 17, 0, 0)

        # Act
        result = format_date_range(start_date, end_date)

        # Assert
        assert isinstance(result, str)
        assert len(result) > 0

    def test_truncate_text_short(self):
        """Test text truncation with short text."""
        # Arrange
        text = "Short text"
        max_length = 20

        # Act
        result = truncate_text(text, max_length)

        # Assert
        assert result == text

    def test_clean_html_tags_no_tags(self):
        """Test HTML tag cleaning with no tags."""
        # Arrange
        text = "Plain text without tags"

        # Act
        result = clean_html_tags(text)

        # Assert
        assert result == text
