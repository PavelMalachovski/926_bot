"""Tests for the Yahoo Finance fetcher: payload parsing and H4 resampling."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.exceptions import DataFetchError
from app.services.smc.models import Candle
from app.services.smc.yahoo import parse_chart_payload, resample_h4


def _payload(timestamps, opens, highs, lows, closes):
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


class TestParsing:
    def test_parses_candles(self):
        base = 1751800000
        payload = _payload(
            [base, base + 300],
            [147.1, 147.2],
            [147.3, 147.4],
            [147.0, 147.1],
            [147.2, 147.3],
        )
        candles = parse_chart_payload(payload)
        assert len(candles) == 2
        assert candles[0].open == 147.1
        assert candles[0].timestamp.tzinfo is not None

    def test_null_rows_skipped(self):
        base = 1751800000
        payload = _payload(
            [base, base + 300],
            [147.1, None],
            [147.3, 147.4],
            [147.0, 147.1],
            [147.2, 147.3],
        )
        assert len(parse_chart_payload(payload)) == 1

    def test_error_payload_raises(self):
        with pytest.raises(DataFetchError):
            parse_chart_payload({"chart": {"result": None, "error": {"code": "Not Found"}}})


class TestResampleH4:
    @staticmethod
    def _h1(start: datetime, values):
        return [
            Candle(
                timestamp=start + timedelta(hours=i),
                open=v,
                high=v + 1,
                low=v - 1,
                close=v + 0.5,
            )
            for i, v in enumerate(values)
        ]

    def test_aggregates_aligned_buckets(self):
        start = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        candles = self._h1(start, [100, 101, 102, 103, 104, 105, 106, 107])
        now = start + timedelta(hours=9)
        h4 = resample_h4(candles, now=now)
        assert len(h4) == 2
        first = h4[0]
        assert first.timestamp == start
        assert first.open == 100  # open of hour 0
        assert first.close == 103.5  # close of hour 3
        assert first.high == 104  # max high (103 + 1)
        assert first.low == 99  # min low (100 - 1)

    def test_incomplete_bucket_dropped(self):
        start = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)
        candles = self._h1(start, [100, 101, 102, 103, 104, 105])  # 6 hours
        now = start + timedelta(hours=6)  # second bucket still open
        h4 = resample_h4(candles, now=now)
        assert len(h4) == 1

    def test_short_final_bucket_counts_after_window(self):
        # Friday-close style: bucket has only 2 hours but its window passed.
        start = datetime(2026, 7, 10, 16, 0, tzinfo=timezone.utc)
        candles = self._h1(start, [100, 101])
        now = start + timedelta(hours=5)
        h4 = resample_h4(candles, now=now)
        assert len(h4) == 1
        assert h4[0].close == 101.5
