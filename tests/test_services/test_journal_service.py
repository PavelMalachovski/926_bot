"""Tests for JournalService (MT4 screenshot trade journal)."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.models import Base
from app.services.journal_service import JournalService

OWNER_ID = 540529430


@pytest.fixture
def journal_service():
    svc = JournalService()
    svc.api_key = "test-key"  # ensure parsing path is enabled
    return svc


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


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
    def test_numeric_and_thousands_separators(self, journal_service):
        t = journal_service._normalize(
            {"symbol": "ethusd", "open_price": "1 814.32", "profit": "-9.40"}
        )
        assert t["symbol"] == "ETHUSD"
        assert t["open_price"] == 1814.32
        assert t["profit"] == -9.40

    def test_missing_values_become_none_or_zero(self, journal_service):
        t = journal_service._normalize({"symbol": "btcusd"})
        assert t["open_price"] is None
        assert t["profit"] == 0.0  # money fields default to 0
        assert t["closed_by_sl"] is False

    def test_datetime_parsing(self, journal_service):
        t = journal_service._normalize({"symbol": "x", "close_time": "2026.06.09 00:01:29"})
        assert t["close_time"] == datetime(2026, 6, 9, 0, 1, 29)

    def test_symbol_defaults_when_absent(self, journal_service):
        t = journal_service._normalize({})
        assert t["symbol"] == "UNKNOWN"


class TestPersistence:
    @pytest.mark.asyncio
    async def test_save_confirm_and_stats(self, journal_service, db_session):
        norm = [journal_service._normalize(t) for t in _sample()]
        batch = await journal_service.save_pending_batch(db_session, OWNER_ID, norm)

        result = await journal_service.confirm_batch(db_session, OWNER_ID, batch)
        assert result == {"saved": 2, "duplicates": 0}

        stats = await journal_service.get_stats(db_session, OWNER_ID)
        assert stats["total"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        # 4.71 + 0.71 swap - 11.69 = -6.27
        assert round(stats["total_net"], 2) == -6.27

    @pytest.mark.asyncio
    async def test_confirm_deduplicates_by_ticket(self, journal_service, db_session):
        norm = [journal_service._normalize(t) for t in _sample()]
        b1 = await journal_service.save_pending_batch(db_session, OWNER_ID, norm)
        await journal_service.confirm_batch(db_session, OWNER_ID, b1)

        # Same trades again -> all duplicates
        b2 = await journal_service.save_pending_batch(db_session, OWNER_ID, norm)
        result = await journal_service.confirm_batch(db_session, OWNER_ID, b2)
        assert result == {"saved": 0, "duplicates": 2}

        stats = await journal_service.get_stats(db_session, OWNER_ID)
        assert stats["total"] == 2  # unchanged

    @pytest.mark.asyncio
    async def test_discard_removes_pending(self, journal_service, db_session):
        norm = [journal_service._normalize(t) for t in _sample()]
        batch = await journal_service.save_pending_batch(db_session, OWNER_ID, norm)

        removed = await journal_service.discard_batch(db_session, OWNER_ID, batch)
        assert removed == 2

        stats = await journal_service.get_stats(db_session, OWNER_ID)
        assert stats.get("total", 0) == 0

    @pytest.mark.asyncio
    async def test_pending_not_counted_in_stats(self, journal_service, db_session):
        norm = [journal_service._normalize(t) for t in _sample()]
        await journal_service.save_pending_batch(db_session, OWNER_ID, norm)
        # No confirm -> nothing confirmed
        stats = await journal_service.get_stats(db_session, OWNER_ID)
        assert stats.get("total", 0) == 0

    @pytest.mark.asyncio
    async def test_stats_isolated_per_user(self, journal_service, db_session):
        norm = [journal_service._normalize(t) for t in _sample()]
        b = await journal_service.save_pending_batch(db_session, OWNER_ID, norm)
        await journal_service.confirm_batch(db_session, OWNER_ID, b)

        other_stats = await journal_service.get_stats(db_session, 111)
        assert other_stats.get("total", 0) == 0


class TestFormatting:
    def test_empty_preview(self, journal_service):
        text = journal_service.format_preview([])
        assert "не удалось распознать" in text

    def test_preview_has_totals(self, journal_service):
        norm = [journal_service._normalize(t) for t in _sample()]
        text = journal_service.format_preview(norm)
        assert "Распознано сделок: 2" in text
        assert "USDJPY" in text and "GBPUSD" in text

    def test_journal_empty(self, journal_service):
        assert "пуст" in journal_service.format_journal({"total": 0})


class TestParseScreenshot:
    @pytest.mark.asyncio
    async def test_parse_screenshot_uses_vision_response(self, journal_service):
        api_payload = {
            "choices": [
                {"message": {"content": json.dumps({"trades": _sample()})}}
            ]
        }
        mock_response = MagicMock()
        mock_response.json.return_value = api_payload
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False

        with patch("app.services.journal_service.httpx.AsyncClient", return_value=mock_client):
            trades = await journal_service.parse_screenshot(b"fake-image-bytes")

        assert len(trades) == 2
        assert trades[0]["symbol"] == "USDJPY"
        assert trades[0]["open_price"] == 160.012

    @pytest.mark.asyncio
    async def test_parse_screenshot_without_api_key_raises(self, journal_service):
        journal_service.api_key = None
        with pytest.raises(RuntimeError):
            await journal_service.parse_screenshot(b"x")
