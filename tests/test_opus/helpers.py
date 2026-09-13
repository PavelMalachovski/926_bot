"""Fixtures for the Opus bot tests: synthetic candles in every timeframe,
a fake Anthropic client that answers with canned JSON, a recording
notifier."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List, Optional

from app.services.smc.models import Candle
from tests.test_smc.helpers import make_candles

# Wednesday 2026-09-09 08:30 UTC = 10:30 Prague (CEST) — London block.
NOW = datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)


def flat_candles(price: float, n: int, step: int, end: datetime = NOW) -> List[Candle]:
    """`n` closed candles ending just before `end`, oscillating gently around
    `price` so pivots exist and nothing is degenerate."""
    closes = [price + ((i % 7) - 3) * 0.05 for i in range(n)]
    start = end - timedelta(minutes=step * n)
    return make_candles(closes, start=start, step_minutes=step)


def market_data(price: float = 147.25, end: datetime = NOW) -> dict:
    return {
        "h4": flat_candles(price, 60, 240, end),
        "h1": flat_candles(price, 120, 60, end),
        "m5": flat_candles(price, 200, 5, end),
    }


GOOD_LIMIT = {
    "action": "limit", "direction": "short", "bias": "short",
    "entry": 148.10, "stop_loss": 148.40, "tp1": 147.40, "tp2": 147.00,
    "invalidation": 146.90, "watch_low": None, "watch_high": None,
    "valid_for": "session", "confidence": 4,
    "read": "Price is in premium after sweeping the Asian high; the H1 supply OB at 148.10 is fresh.",
    "reasons": ["H4 lower highs", "H1 OB 148.05-148.15 untested"],
    "risks": ["CPI at 14:30"],
}

GOOD_WAIT = {
    "action": "wait", "direction": "none", "bias": "long",
    "entry": None, "stop_loss": None, "tp1": None, "tp2": None,
    "invalidation": 146.80, "watch_low": 146.90, "watch_high": 147.05,
    "valid_for": "day", "confidence": 3,
    "read": "No displacement yet; the H1 demand at 146.90-147.05 is where a long would set up.",
    "reasons": ["H4 higher lows"], "risks": ["NY open sweep"],
}


class FakeMessages:
    def __init__(self, answers: List[Optional[dict]], stop_reason: str = "end_turn"):
        self.answers = list(answers)
        self.stop_reason = stop_reason
        self.calls: List[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if self.answers else None
        text = "" if answer is None else (
            answer if isinstance(answer, str) else json.dumps(answer)
        )
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
            model="claude-opus-5",
            _request_id="req_test",
        )


class FakeClient:
    """`client.beta.messages.create` and `client.messages.create` share one
    answer queue so a test can script the retry."""

    def __init__(self, answers, stop_reason: str = "end_turn"):
        self.messages = FakeMessages(answers, stop_reason)
        self.beta = SimpleNamespace(messages=self.messages)

    @property
    def calls(self):
        return self.messages.calls


class RecordingNotifier:
    def __init__(self):
        self.sent: List[str] = []
        self.photos: List[bytes] = []
        self._next_id = 100

    async def send(self, text, reply_markup=None, disable_notification=False):
        self.sent.append(text)
        self._next_id += 1
        return self._next_id

    async def send_photo(self, photo, caption=None, reply_to=None):
        self.photos.append(photo)
        return self._next_id + 1000

    async def edit_message(self, message_id, text, reply_markup=None):
        return True
