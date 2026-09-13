"""The analyst's call contract on a fake client: one call when the answer
passes, a second call carrying the violations when it does not, a
downgrade when the second still fails, None on every failure."""

import asyncio

import pytest

from app.services.opus.analyst import FALLBACK_BETA, OpusAnalyst, SYSTEM_PROMPT
from app.services.opus.decision import DECISION_SCHEMA, Limits
from tests.test_opus.helpers import FakeClient, GOOD_LIMIT, GOOD_WAIT

LIMITS = Limits(price=147.25, min_rr=2.0, session_open=True, decimals=3)


def analyst(*answers, **kw) -> OpusAnalyst:
    return OpusAnalyst(model="claude-opus-5", client=FakeClient(list(answers)), **kw)


class TestCall:
    def test_good_answer_is_one_call(self):
        a = analyst(GOOD_LIMIT)
        out = asyncio.run(a.decide("brief", [b"png"], LIMITS))
        assert out.decision.action == "limit" and out.attempts == 1
        call = a._client.calls[0]
        assert call["model"] == "claude-opus-5"
        assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
        assert call["output_config"]["format"]["schema"] is DECISION_SCHEMA
        assert call["output_config"]["effort"] == "high"
        assert call["thinking"] == {"type": "adaptive"}
        assert call["system"][0]["text"] == SYSTEM_PROMPT
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        content = call["messages"][0]["content"]
        assert content[0]["type"] == "image" and content[-1]["type"] == "text"
        assert "brief" in content[-1]["text"] and "English" in content[-1]["text"]

    def test_violation_triggers_one_retry_with_the_reasons(self):
        thin = {**GOOD_LIMIT, "tp1": 147.70}
        a = analyst(thin, GOOD_LIMIT)
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision.action == "limit" and out.attempts == 2
        assert out.decision.downgraded_from is None
        retry = a._client.calls[1]["messages"][0]["content"][-1]["text"]
        assert "YOUR PREVIOUS ANSWER broke these hard limits" in retry
        assert "RR to tp1 is 1:1.3" in retry
        assert out.first_violations and "RR" in out.first_violations[0]

    def test_second_failure_is_downgraded(self):
        thin = {**GOOD_LIMIT, "tp1": 147.70}
        a = analyst(thin, thin)
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision.action == "wait" and out.decision.downgraded_from == "limit"
        assert out.decision.entry == 148.10 and any("RR" in n for n in out.decision.notes)

    def test_unparsable_retry_keeps_the_first_idea_downgraded(self):
        thin = {**GOOD_LIMIT, "tp1": 147.70}
        a = analyst(thin, "garbage")
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision.action == "wait" and out.decision.downgraded_from == "limit"

    def test_wait_never_retries(self):
        a = analyst(GOOD_WAIT)
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision.action == "wait" and len(a._client.calls) == 1


class TestFailures:
    def test_no_key(self):
        a = OpusAnalyst(api_key=None)
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision is None and out.error == "no ANTHROPIC_API_KEY"

    def test_unparsable(self):
        a = analyst("nope")
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision is None and out.error == "unparsable"

    def test_refusal(self):
        a = OpusAnalyst(client=FakeClient([GOOD_LIMIT], stop_reason="refusal"))
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision is None and out.error == "refusal"

    def test_empty_answer_names_max_tokens(self):
        a = OpusAnalyst(client=FakeClient([None], stop_reason="max_tokens"))
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision is None and out.error == "max_tokens"

    def test_api_error(self):
        class Boom:
            async def create(self, **kw):
                raise RuntimeError("503 overloaded")

        from types import SimpleNamespace

        client = SimpleNamespace(messages=Boom(), beta=SimpleNamespace(messages=Boom()))
        a = OpusAnalyst(client=client, fallbacks=False)
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision is None and out.error.startswith("api: 503")

    def test_unknown_fallback_beta_falls_back_to_plain_call(self):
        from types import SimpleNamespace

        plain = FakeClient([GOOD_WAIT]).messages

        class Rejects:
            async def create(self, **kw):
                raise RuntimeError("400 unknown beta: server-side-fallback")

        client = SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=Rejects()))
        a = OpusAnalyst(client=client)
        out = asyncio.run(a.decide("brief", [], LIMITS))
        assert out.decision.action == "wait"
        assert a.fallbacks is False and "betas" not in plain.calls[0]

    def test_language_instruction_follows_the_bot(self):
        from app.services.smc import i18n

        i18n.set_language("ru")
        try:
            a = analyst(GOOD_WAIT)
            asyncio.run(a.decide("brief", [], LIMITS))
            assert "по-русски" in a._client.calls[0]["messages"][0]["content"][-1]["text"]
        finally:
            i18n.set_language("en")
