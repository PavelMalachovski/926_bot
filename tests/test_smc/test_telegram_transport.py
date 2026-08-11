"""Regression tests for the getUpdates transport: JSON guard, boot guard,
and refused-poll backoff (review-hardening Task 3).

All network-free: httpx.AsyncClient is monkeypatched to a fake client that
answers from a canned queue, and `asyncio.sleep` is monkeypatched to a spy
so backoff timing is asserted without ever actually sleeping.
"""

import httpx
import pytest

from app.services.smc import telegram_bot as tb
from app.services.smc.telegram_bot import TelegramCommandBot


def _bot() -> TelegramCommandBot:
    """A bare bot instance — enough attributes for `_api`/`run`/`_poll_once`,
    none of the constructor's optional callables (this suite never reaches
    command routing)."""
    bot = TelegramCommandBot.__new__(TelegramCommandBot)
    bot.bot_token = "123:TEST"
    bot.base_url = "https://api.telegram.org/bot123:TEST"
    bot.owner_chat_id = "1"
    bot._offset = None
    return bot


def _fake_client_factory(queue):
    """httpx.AsyncClient stand-in whose `.post()` pops the next canned
    response (or exception) off `queue`. No network."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return _FakeClient


class _FakeResponse:
    """A response whose `.json()` either raises (non-JSON body) or returns
    a canned payload."""

    def __init__(self, payload=None, raise_json=False):
        self._payload = payload if payload is not None else {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _Done(Exception):
    """Sentinel used to break out of `run()`'s `while True` from inside a
    stubbed `_poll_once`, so the loop is observably entered without running
    forever."""


# --------------------------------------------------------------- (a) _api


class TestApiNeverRaises:
    @pytest.mark.asyncio
    async def test_non_json_body_returns_none(self, monkeypatch):
        # A proxy 502 with an HTML error page instead of JSON.
        monkeypatch.setattr(
            tb.httpx,
            "AsyncClient",
            _fake_client_factory([_FakeResponse(raise_json=True)]),
        )
        bot = _bot()
        result = await bot._api("getMe")
        assert result is None

    @pytest.mark.asyncio
    async def test_transport_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            tb.httpx,
            "AsyncClient",
            _fake_client_factory([httpx.ConnectError("DNS failure")]),
        )
        bot = _bot()
        result = await bot._api("getMe")
        assert result is None

    @pytest.mark.asyncio
    async def test_ok_false_returns_none_but_does_not_raise(self, monkeypatch):
        # 401 revoked token / 409 second-instance overlap: the body parses,
        # ok is false — still a failure, never an exception.
        monkeypatch.setattr(
            tb.httpx,
            "AsyncClient",
            _fake_client_factory(
                [_FakeResponse({"ok": False, "error_code": 409,
                                "description": "Conflict"})]
            ),
        )
        bot = _bot()
        result = await bot._api("getUpdates")
        assert result is None

    @pytest.mark.asyncio
    async def test_ok_true_returns_the_result_payload(self, monkeypatch):
        monkeypatch.setattr(
            tb.httpx,
            "AsyncClient",
            _fake_client_factory([_FakeResponse({"ok": True, "result": [1, 2]})]),
        )
        bot = _bot()
        result = await bot._api("getUpdates")
        assert result == [1, 2]


# --------------------------------------------------------- (b) boot guard


class TestRunSurvivesAFlakyBoot:
    @pytest.mark.asyncio
    async def test_deleteWebhook_raising_still_enters_the_polling_loop(
        self, monkeypatch
    ):
        calls = []

        async def fake_api(method, **kwargs):
            calls.append(method)
            if method == "deleteWebhook":
                raise httpx.ConnectError("boot is flaky")
            return {}

        entered = []

        async def fake_poll_once():
            entered.append(True)
            raise _Done()

        bot = _bot()
        bot._api = fake_api
        bot._poll_once = fake_poll_once

        with pytest.raises(_Done):
            await bot.run()

        assert calls == ["deleteWebhook"]  # _setup_bot_profile never reached
        assert entered == [True]  # but the polling loop still ran


# ------------------------------------------------------------- (c)/(d) backoff


class TestGetUpdatesBackoff:
    @pytest.mark.asyncio
    async def test_refused_polls_back_off_then_reset_on_success(self, monkeypatch):
        # False=refused, True=success. Three refusals, a success, then one
        # more refusal to prove the counter actually reset.
        results = [False, False, False, True, False]
        state = {"n": 0}

        async def fake_poll_once():
            if state["n"] >= len(results):
                raise _Done()
            value = results[state["n"]]
            state["n"] += 1
            return value

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        bot = _bot()
        bot._poll_once = fake_poll_once

        async def noop_api(method, **kwargs):
            return {}

        bot._api = noop_api
        monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)

        with pytest.raises(_Done):
            await bot.run()

        assert sleeps == [5.0, 10.0, 20.0, 5.0]

    @pytest.mark.asyncio
    async def test_successful_empty_poll_does_not_trigger_backoff_sleep(
        self, monkeypatch
    ):
        # A successful poll with zero updates is not a failure: Telegram's
        # own 30s long-poll timeout is the cadence, so `run()` loops straight
        # back into the next `_poll_once()` with no extra sleep at all.
        results = [True, True]
        state = {"n": 0}

        async def fake_poll_once():
            if state["n"] >= len(results):
                raise _Done()
            value = results[state["n"]]
            state["n"] += 1
            return value

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        bot = _bot()
        bot._poll_once = fake_poll_once

        async def noop_api(method, **kwargs):
            return {}

        bot._api = noop_api
        monkeypatch.setattr(tb.asyncio, "sleep", fake_sleep)

        with pytest.raises(_Done):
            await bot.run()

        assert sleeps == []


# ------------------------------------------------------ _poll_once (unit)


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_refused_poll_returns_false(self, monkeypatch):
        bot = _bot()

        async def fake_api(method, **kwargs):
            assert method == "getUpdates"
            return None

        bot._api = fake_api
        assert await bot._poll_once() is False

    @pytest.mark.asyncio
    async def test_empty_success_returns_true_and_leaves_offset(self):
        bot = _bot()

        async def fake_api(method, **kwargs):
            return []

        bot._api = fake_api
        assert await bot._poll_once() is True
        assert bot._offset is None

    @pytest.mark.asyncio
    async def test_updates_are_dispatched_and_offset_advances(self):
        bot = _bot()
        handled = []

        async def fake_api(method, **kwargs):
            return [{"update_id": 42, "message": {}}]

        async def fake_handle_update(update):
            handled.append(update["update_id"])

        bot._api = fake_api
        bot._handle_update = fake_handle_update
        assert await bot._poll_once() is True
        assert handled == [42]
        assert bot._offset == 43
