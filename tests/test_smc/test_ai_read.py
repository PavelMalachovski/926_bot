"""The AI read (owner decision D26, 2026-09-06): Claude's second opinion,
appended to the 🚨 card and stored with the audit. Comment only, best-effort
everywhere, no invented numbers — the fact sheet is the model's only source
of levels, and every failure leaves the bot exactly as it was before D26."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.smc.ai_read import (
    AIRead,
    AIReader,
    READ_SCHEMA,
    describe_for_ai,
    parse_ai_read,
)
from app.services.smc.db import Database
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.models import AnalysisResult, Verdict
from app.services.smc.notifier import format_ai_read
from app.services.smc.pending import build_pending
from app.services.smc.planbook import PlanBook, PlanEntry
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)

ETH = get_instrument("ETHUSD")
T0 = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)


def _evaluated(m5):
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=T0)
    r.session_name = "Frankfurt/London"
    r.price = m5[-1].close
    r = TripleSyncEngine(max_entry_gap_r=99.0).evaluate(h4=h4, h1=h1, m5=m5, result=r)
    r.h4_candles, r.h1_candles, r.m5_candles = h4, h1, m5
    return r, h4, h1, m5


GOOD = {
    "stance": "caution",
    "preferred_entry": "deep",
    "confidence": 4,
    "read": "Context agrees; the first pool is close, so the deeper limit pays better.",
    "risks": ["EQH at 3305 may be swept first", "H1 still flat"],
}


# --------------------------------------------------------------- fact sheet


class TestFactSheet:
    def test_a_formed_setup_lists_every_number_the_card_prints(self):
        result, h4, h1, m5 = _evaluated(m5_long_trigger())
        audit = build_pending(result, ETH, h4, h1, m5)
        text = describe_for_ai(result, ETH, audit=audit)
        s = result.setup
        assert "SETUP FORMED: LONG" in text
        assert f"{s.entry:.2f}" in text and f"{s.stop_loss:.2f}" in text
        assert "Unswept liquidity ahead:" in text
        assert "Pending entry [main]" in text and "Pending entry [deep]" in text
        assert "Market reference:" in text
        assert "None" not in text

    def test_a_watch_states_the_checklist_stage(self):
        result, *_ = _evaluated(make_candles([3160.0]))
        text = describe_for_ai(result, ETH)
        assert "Checklist state: watch" in text
        assert "H1 zone of interest: demand OB 3131.00-3138.00" in text
        assert "SETUP FORMED" not in text


# ------------------------------------------------------------------ parsing


class TestParse:
    def test_valid_json_becomes_a_read(self):
        read = parse_ai_read(json.dumps(GOOD), model="m", as_of="08:05")
        assert read == AIRead(
            stance="caution", preferred_entry="deep", confidence=4,
            read=GOOD["read"], risks=GOOD["risks"], model="m", as_of="08:05",
        )

    def test_bad_stance_or_entry_is_dropped_whole(self):
        assert parse_ai_read({**GOOD, "stance": "maybe"}) is None
        assert parse_ai_read({**GOOD, "preferred_entry": "limit"}) is None
        assert parse_ai_read({**GOOD, "read": ""}) is None

    def test_confidence_is_clamped_and_risks_capped_at_three(self):
        read = parse_ai_read({**GOOD, "confidence": 9, "risks": list("abcd")})
        assert read.confidence == 5 and read.risks == ["a", "b", "c"]

    def test_garbage_is_none(self):
        assert parse_ai_read("not json") is None
        assert parse_ai_read(["list"]) is None

    def test_schema_is_closed(self):
        assert READ_SCHEMA["additionalProperties"] is False
        assert set(READ_SCHEMA["required"]) == set(READ_SCHEMA["properties"])


# ------------------------------------------------------------------- reader


class _FakeClient:
    def __init__(self, reply=None, error=None, stop_reason="end_turn"):
        self.calls = []
        self.reply = reply
        self.error = error
        self.stop_reason = stop_reason
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=json.dumps(self.reply or GOOD))],
        )


class TestReader:
    def test_no_key_means_disabled_and_no_call(self):
        reader = AIReader(api_key=None)
        assert not reader.enabled
        assert asyncio.run(reader.read("facts", images=[b"png"])) is None

    def test_request_shape(self):
        client = _FakeClient()
        reader = AIReader(model="claude-sonnet-5", effort="low", client=client)
        read = asyncio.run(reader.read("Price: 3150.00", images=[b"\\x89PNG", b""], as_of="08:05"))
        assert read is not None and read.model == "claude-sonnet-5" and read.as_of == "08:05"
        call = client.calls[0]
        assert call["model"] == "claude-sonnet-5"
        assert call["thinking"] == {"type": "adaptive"}
        assert call["output_config"]["effort"] == "low"
        assert call["output_config"]["format"] == {"type": "json_schema", "schema": READ_SCHEMA}
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        content = call["messages"][0]["content"]
        assert [b["type"] for b in content] == ["image", "text"]  # the empty PNG was skipped
        assert "Price: 3150.00" in content[1]["text"]

    def test_api_failure_is_none_not_an_exception(self):
        reader = AIReader(client=_FakeClient(error=RuntimeError("boom")))
        assert asyncio.run(reader.read("facts")) is None

    def test_refusal_is_none(self):
        reader = AIReader(client=_FakeClient(stop_reason="refusal"))
        assert asyncio.run(reader.read("facts")) is None

    def test_unparsable_answer_is_none(self):
        reader = AIReader(client=_FakeClient(reply={"stance": "agree"}))
        assert asyncio.run(reader.read("facts")) is None


# --------------------------------------------------------------- formatting


def test_block_escapes_every_model_field():
    read = AIRead(
        stance="against", preferred_entry="wait", confidence=2,
        read="Price is <above> the range & the pool", risks=["a <b>"],
        model="claude-sonnet-5", as_of="08:05",
    )
    text = format_ai_read(read)
    assert text.startswith("🧠 <b>AI read</b> (claude-sonnet-5 · 08:05 Prague): AGAINST · confidence 2/5 · prefers WAIT")
    assert "&lt;above&gt; the range &amp; the pool" in text
    assert "⚠️ a &lt;b&gt;" in text


# ----------------------------------------------------------------- watcher


class _Notifier:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send(self, text, reply_markup=None, disable_notification=False):
        self.sent.append(text)
        return len(self.sent)

    async def edit_message(self, message_id, text, reply_markup=None):
        self.edited.append((message_id, text, reply_markup))
        return True

    async def send_photo(self, photo, caption=None, reply_to=None):
        return 99

    async def pin(self, message_id):
        pass


class _StubReader:
    def __init__(self, read=None, error=None):
        self.read_value = read
        self.error = error
        self.facts = []
        self.enabled = True
        self.model = "stub"

    async def read(self, facts, images=(), as_of=""):
        self.facts.append((facts, list(images), as_of))
        if self.error:
            raise self.error
        return self.read_value


def _watcher(tmp_path, reader):
    from smc_watcher import Watcher

    db = Database(str(tmp_path / "smc.db"))
    w = Watcher.__new__(Watcher)
    w.db = db
    w.state = WatcherState(db)
    w.state.pairs = ["ETHUSD"]
    w.journal = SignalJournal(db)
    w.notifier = _Notifier()
    w.news = None
    w.last_results = {}
    w.planbook = PlanBook()
    w.ai = reader

    async def _chart(result, reply_to):
        return b"m5png"

    w._send_chart = _chart
    return w


READ = AIRead(
    stance="caution", preferred_entry="deep", confidence=4,
    read="Context agrees.", risks=["pool close"], model="stub", as_of="10:30",
)


class TestAlertRead:
    @pytest.mark.asyncio
    async def test_the_block_is_appended_by_editing_the_sent_card(self, tmp_path):
        reader = _StubReader(read=READ)
        w = _watcher(tmp_path, reader)
        result, *_ = _evaluated(m5_long_trigger())
        assert await w._send_alert("ETHUSD", result, "fp") is True

        assert len(w.notifier.sent) == 1  # the alert went out first, unchanged
        card = w.notifier.sent[0]
        assert "🧠" not in card
        message_id, text, markup = w.notifier.edited[-1]
        assert message_id == 1 and text.startswith(card)
        assert "🧠 <b>AI read</b> (stub · 10:30 Prague): CAUTION · confidence 4/5 · prefers DEEP" in text
        assert markup["inline_keyboard"][0][0]["callback_data"].startswith("take_")
        # the live card re-renders from the stored text, so it carries the block
        assert w.journal.signals[0]["alert_text"] == text
        facts, images, _ = reader.facts[0]
        assert "SETUP FORMED: LONG" in facts and images == [b"m5png"]

    @pytest.mark.asyncio
    async def test_a_failed_read_leaves_the_card_as_sent(self, tmp_path):
        w = _watcher(tmp_path, _StubReader(error=RuntimeError("down")))
        result, *_ = _evaluated(m5_long_trigger())
        assert await w._send_alert("ETHUSD", result, "fp") is True
        assert w.notifier.edited == []
        assert w.journal.signals[0]["alert_text"] == w.notifier.sent[0]

    @pytest.mark.asyncio
    async def test_no_reader_means_no_edit(self, tmp_path):
        w = _watcher(tmp_path, None)
        result, *_ = _evaluated(m5_long_trigger())
        assert await w._send_alert("ETHUSD", result, "fp") is True
        assert w.notifier.edited == []


class TestAuditRead:
    def _entry(self):
        result, h4, h1, m5 = _evaluated(make_candles([3160.0]))
        from app.services.smc.plan import build_plan

        plan = build_plan(ETH, h4, h1, m5)
        return PlanEntry(
            plan=plan, data={"h4": h4, "h1": h1, "m5": m5}, as_of="08:04",
            result=result, audit=build_pending(result, ETH, h4, h1, m5),
        )

    def test_audit_read_uses_the_pending_entries(self, tmp_path):
        reader = _StubReader(read=READ)
        w = _watcher(tmp_path, reader)
        entry = self._entry()
        w.planbook.update("ETHUSD", entry)
        read = asyncio.run(w._ai_read_audit("ETHUSD", entry))
        assert read is READ
        facts = reader.facts[0][0]
        assert "Pending entry [main] H1 Demand OB" in facts

    def test_recompute_carries_the_read_forward(self, tmp_path):
        w = _watcher(tmp_path, _StubReader(read=READ))
        entry = self._entry()
        entry.ai_read = READ
        w.planbook.update("ETHUSD", entry)
        w._recompute_plan("ETHUSD", entry.result)
        assert w.planbook.get("ETHUSD").ai_read is READ

    def test_the_audit_message_carries_the_block(self, tmp_path, monkeypatch):
        import app.services.smc.chart as chart_mod

        monkeypatch.setattr(chart_mod, "render_plan_chart", lambda *a, **k: None)
        w = _watcher(tmp_path, None)
        entry = self._entry()
        entry.ai_read = READ
        w.planbook.update("ETHUSD", entry)
        asyncio.run(w._send_setup_analysis("ETHUSD", fresh=False))
        text = w.notifier.sent[0]
        assert "Strategy audit — ETHUSD" in text
        assert "🧠 <b>AI read</b> (stub · 10:30 Prague)" in text
