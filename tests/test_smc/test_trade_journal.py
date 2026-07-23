"""Tests for the manual trade journal (MT4 screenshot parsing + stats)."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.smc.db import Database
from app.services.smc.trade_journal import TradeJournal


def _journal(tmp_path) -> TradeJournal:
    return TradeJournal(Database(str(tmp_path / "smc.db")))


def _sample():
    return [
        {
            "ticket": "71119736",
            "symbol": "usdjpy",
            "direction": "buy",
            "volume": 0.13,
            "open_price": "160.012",
            "close_price": "160.079",
            "open_time": "2026.06.08 11:57:29",
            "close_time": "2026.06.09 00:01:29",
            "sl": "160.079",
            "tp": "160.400",
            "profit": "4.71",
            "swap": "0.71",
            "closed_by_sl": True,
        },
        {
            "ticket": "71136415",
            "symbol": "gbpusd",
            "direction": "sell",
            "volume": 0.13,
            "open_price": "1.34018",
            "close_price": "1.34122",
            "close_time": "2026.06.10 16:35:20",
            "profit": "-11.69",
            "closed_by_sl": True,
        },
    ]


class TestNormalize:
    def test_thousands_separators_stripped(self, tmp_path):
        tj = _journal(tmp_path)
        t = tj._normalize({"symbol": "ethusd", "open_price": "1 814.32", "profit": "-9.40"})
        assert t["symbol"] == "ETHUSD"
        assert t["open_price"] == 1814.32
        assert t["profit"] == -9.40

    def test_missing_values(self, tmp_path):
        tj = _journal(tmp_path)
        t = tj._normalize({"symbol": "btcusd"})
        assert t["open_price"] is None
        assert t["profit"] == 0.0
        assert t["closed_by_sl"] is False

    def test_datetime_normalized_to_iso(self, tmp_path):
        tj = _journal(tmp_path)
        t = tj._normalize({"symbol": "x", "close_time": "2026.06.09 00:01:29"})
        assert t["close_time"] == "2026-06-09 00:01:29"

    def test_symbol_defaults(self, tmp_path):
        tj = _journal(tmp_path)
        assert tj._normalize({})["symbol"] == "UNKNOWN"


class TestPersistence:
    def test_save_confirm_and_stats(self, tmp_path):
        tj = _journal(tmp_path)
        norm = [tj._normalize(t) for t in _sample()]
        batch = tj.save_pending_batch(norm)
        assert tj.confirm_batch(batch) == {"saved": 2, "duplicates": 0}

        stats = tj.get_stats()
        assert stats["total"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        # 4.71 + 0.71 swap - 11.69 = -6.27
        assert round(stats["total_net"], 2) == -6.27

    def test_confirm_deduplicates_by_ticket(self, tmp_path):
        tj = _journal(tmp_path)
        norm = [tj._normalize(t) for t in _sample()]
        tj.confirm_batch(tj.save_pending_batch(norm))

        result = tj.confirm_batch(tj.save_pending_batch(norm))
        assert result == {"saved": 0, "duplicates": 2}
        assert tj.get_stats()["total"] == 2

    def test_discard_removes_pending(self, tmp_path):
        tj = _journal(tmp_path)
        norm = [tj._normalize(t) for t in _sample()]
        batch = tj.save_pending_batch(norm)
        assert tj.discard_batch(batch) == 2
        assert tj.get_stats().get("total", 0) == 0

    def test_pending_not_counted(self, tmp_path):
        tj = _journal(tmp_path)
        norm = [tj._normalize(t) for t in _sample()]
        tj.save_pending_batch(norm)  # no confirm
        assert tj.get_stats().get("total", 0) == 0


class TestFormatting:
    def test_empty_preview(self, tmp_path):
        assert "не удалось распознать" in _journal(tmp_path).format_preview([])

    def test_preview_has_totals(self, tmp_path):
        tj = _journal(tmp_path)
        norm = [tj._normalize(t) for t in _sample()]
        text = tj.format_preview(norm)
        assert "Распознано сделок: 2" in text
        assert "USDJPY" in text and "GBPUSD" in text

    def test_journal_empty(self, tmp_path):
        assert "пуст" in _journal(tmp_path).format_journal({"total": 0})

    def test_stats_text_after_confirm(self, tmp_path):
        tj = _journal(tmp_path)
        norm = [tj._normalize(t) for t in _sample()]
        tj.confirm_batch(tj.save_pending_batch(norm))
        text = tj.stats_text()
        assert "Журнал сделок" in text
        assert "Win rate" in text


class TestParseScreenshot:
    @pytest.mark.asyncio
    async def test_parse_uses_vision_response(self, tmp_path, monkeypatch):
        tj = _journal(tmp_path)
        monkeypatch.setattr(settings.openai, "api_key", "test-key")

        api_payload = {
            "choices": [{"message": {"content": json.dumps({"trades": _sample()})}}]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = api_payload
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False

        with patch(
            "app.services.smc.trade_journal.httpx.AsyncClient",
            return_value=mock_client,
        ):
            trades = await tj.parse_screenshot(b"fake-image-bytes")

        assert len(trades) == 2
        assert trades[0]["symbol"] == "USDJPY"
        assert trades[0]["open_price"] == 160.012

    @pytest.mark.asyncio
    async def test_parse_without_api_key_raises(self, tmp_path, monkeypatch):
        tj = _journal(tmp_path)
        monkeypatch.setattr(settings.openai, "api_key", None)
        with pytest.raises(RuntimeError):
            await tj.parse_screenshot(b"x")
