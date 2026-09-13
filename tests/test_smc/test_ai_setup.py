"""The AI setup (owner decision D28, 2026-09-13): Claude proposes ONE
order over the engine's own levels, the code snaps and prices it, the
chart draws it, and the 🚨 card goes limit-first once price has run past
the rung. Still a comment: nothing here changes what the engine announces."""

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.smc import i18n
from app.services.smc.ai_read import (
    AIProposal,
    AIRead,
    READ_SCHEMA,
    LevelCatalog,
    build_catalog,
    describe_for_ai,
    parse_ai_read,
    validate_proposal,
)
from app.services.smc.chart import render_plan_chart, render_setup_chart
from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.models import AnalysisResult, Direction, Verdict
from app.services.smc.notifier import (
    format_ai_read,
    format_result,
    format_setup_analysis,
    set_ai_floor,
)
from app.services.smc.pending import build_pending
from app.services.smc.plan import build_plan
from app.services.smc.planbook import (
    PlanBook,
    PlanEntry,
    match_primary_plan,
    primary_plan_snapshot,
)
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    m5_long_trigger,
    make_candles,
)
from tests.test_smc.test_ai_read import _StubReader, _watcher

ETH = get_instrument("ETHUSD")
T0 = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)


def _evaluated(m5=None, max_gap_r: float = 0.75):
    m5 = m5 or m5_long_trigger()
    h4 = make_candles(H4_UPTREND_CLOSES, step_minutes=240)
    h1 = make_candles(H1_PULLBACK_CLOSES, step_minutes=60)
    r = AnalysisResult(symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=T0)
    r.session_name = "Frankfurt/London"
    r.price = m5[-1].close
    r = TripleSyncEngine(max_entry_gap_r=max_gap_r).evaluate(h4=h4, h1=h1, m5=m5, result=r)
    r.h4_candles, r.h1_candles, r.m5_candles = h4, h1, m5
    return r, h4, h1, m5


def _catalog(max_gap_r: float = 0.75):
    result, h4, h1, m5 = _evaluated(max_gap_r=max_gap_r)
    audit = build_pending(result, ETH, h4, h1, m5)
    return result, audit, build_catalog(result, ETH, audit)


def _good_proposal(result):
    s = result.setup
    return {
        "order": "limit", "direction": "long", "basis": "m5_fvg",
        "entry": s.entry, "stop": s.stop_loss, "target": s.ladder[0].price,
        "invalidation": "M5 body close below the stop",
    }


# ------------------------------------------------------------------ engine


class TestStaleFlag:
    def test_the_fixture_setup_is_stale_past_075r(self):
        result, *_ = _evaluated()
        s = result.setup
        assert s.stale is True
        assert s.entry_gap_r == pytest.approx(
            (result.price - s.entry) / (s.entry - s.stop_loss), abs=0.01
        )
        assert any("past the imbalance" in w for w in result.warnings)

    def test_a_wide_gap_allowance_is_not_stale(self):
        result, *_ = _evaluated(max_gap_r=99.0)
        assert result.setup.stale is False
        assert result.setup.entry_gap_r > 0  # the number is still measured


# ----------------------------------------------------------------- catalog


class TestCatalog:
    def test_formed_setup_lists_every_rung_stop_and_pool(self):
        result, audit, cat = _catalog()
        s = result.setup
        bases = [b for b, _, _ in cat.entries]
        assert cat.direction == Direction.LONG
        assert "market" in bases and "m5_fvg" in bases and "h1_zone" in bases
        fvg = next(b for b in cat.entries if b[0] == "m5_fvg")
        assert fvg[1] == s.fvg.bottom and fvg[2] == s.fvg.top
        assert s.stop_loss in cat.stops
        assert all(lv.price in cat.targets for lv in s.ladder)
        assert cat.tolerance == ETH.min_fvg

    def test_a_watch_state_has_the_zone_and_no_direction_lock_when_mid_range(self):
        result, h4, h1, m5 = _evaluated(m5=make_candles([3160.0]))
        assert result.verdict == Verdict.WATCH
        audit = build_pending(result, ETH, h4, h1, m5)
        cat = build_catalog(result, ETH, audit)
        assert any(b == "h1_zone" for b, _, _ in cat.entries)
        assert cat.stops and cat.targets

    def test_the_fact_sheet_prints_the_allowed_lists_candles_and_news(self):
        result, audit, cat = _catalog()
        text = describe_for_ai(
            result, ETH, audit=audit, catalog=cat, min_rr=2.0,
            news="Next red news: USD CPI at 14:30 Prague (in 95 min)",
        )
        assert "ALLOWED ENTRY BANDS" in text and "ALLOWED STOPS" in text
        assert "ALLOWED TARGETS" in text and "MINIMUM RR: 1:2.0" in text
        assert "Recent M5 candles" in text and "Recent H1 candles" in text
        assert "Next red news: USD CPI" in text
        assert "Trade direction is fixed: LONG" in text

    def test_candles_can_be_left_out(self):
        result, audit, cat = _catalog()
        text = describe_for_ai(result, ETH, audit=audit, candles=False)
        assert "Recent M5 candles" not in text and "ALLOWED" not in text


# -------------------------------------------------------------- validation


class TestValidateProposal:
    def test_a_limit_at_the_fvg_edge_is_kept_and_priced_by_the_code(self):
        result, audit, cat = _catalog()
        raw = _good_proposal(result)
        raw["entry"] += cat.tolerance / 2  # a hair outside the band: pulled to the edge
        p, note = validate_proposal(raw, cat, min_rr=2.0)
        assert note == "" and p is not None and p.is_trade
        s = result.setup
        assert p.entry == pytest.approx(s.fvg.top) or p.entry == pytest.approx(s.entry)
        assert p.stop == s.stop_loss and p.target == s.ladder[0].price
        expected_rr = (p.target - p.entry) / (p.entry - p.stop)
        assert p.rr == pytest.approx(expected_rr, abs=0.01)
        assert p.basis == "m5_fvg" and p.order == "limit"

    def test_an_entry_outside_every_band_is_rejected(self):
        result, audit, cat = _catalog()
        raw = {**_good_proposal(result), "entry": result.setup.entry - 500}
        p, note = validate_proposal(raw, cat)
        assert p is None and "outside" in note

    def test_an_unlisted_stop_or_target_is_rejected(self):
        result, audit, cat = _catalog()
        p, note = validate_proposal({**_good_proposal(result), "stop": 1.0}, cat)
        assert p is None and "stop" in note
        p, note = validate_proposal({**_good_proposal(result), "target": 99999.0}, cat)
        assert p is None and "target" in note

    def test_the_wrong_direction_is_rejected(self):
        result, audit, cat = _catalog()
        p, note = validate_proposal({**_good_proposal(result), "direction": "short"}, cat)
        assert p is None and "direction" in note

    def test_market_far_from_price_becomes_a_limit(self):
        result, audit, cat = _catalog()
        p, _ = validate_proposal({**_good_proposal(result), "order": "market"}, cat)
        assert p is not None and p.order == "limit"

    def test_a_buy_limit_above_price_is_rejected(self):
        result, audit, cat = _catalog()
        cat.entries.append(("zone_next", cat.price + 50, cat.price + 60))
        raw = {**_good_proposal(result), "entry": cat.price + 55, "basis": "zone_next"}
        p, note = validate_proposal(raw, cat)
        assert p is None and "wrong side of price" in note

    def test_below_the_floor_is_flagged_not_dropped(self):
        result, audit, cat = _catalog()
        p, _ = validate_proposal(_good_proposal(result), cat, min_rr=99.0)
        assert p is not None and p.below_floor is True

    def test_none_is_an_explicit_answer(self):
        result, audit, cat = _catalog()
        p, note = validate_proposal(
            {"order": "none", "direction": "none", "basis": "none",
             "entry": None, "stop": None, "target": None,
             "invalidation": "wait for the sweep"}, cat,
        )
        assert p is not None and not p.is_trade and p.invalidation == "wait for the sweep"

    def test_roundtrip_through_dict(self):
        result, audit, cat = _catalog()
        p, _ = validate_proposal(_good_proposal(result), cat)
        again = AIProposal.from_dict(p.to_dict())
        assert again == p
        assert AIProposal.from_dict({"order": "bogus"}) is None


class TestParse:
    def test_schema_carries_the_proposal(self):
        assert "proposal" in READ_SCHEMA["properties"]
        assert "proposal" in READ_SCHEMA["required"]
        assert READ_SCHEMA["properties"]["proposal"]["additionalProperties"] is False

    def test_the_proposal_survives_parsing_with_a_catalog(self):
        result, audit, cat = _catalog()
        payload = {
            "stance": "agree", "preferred_entry": "main", "confidence": 4,
            "read": "ok", "risks": [], "proposal": _good_proposal(result),
        }
        read = parse_ai_read(payload, catalog=cat, min_rr=2.0)
        assert read is not None and read.proposal is not None and read.proposal.is_trade

    def test_without_a_catalog_no_price_reaches_the_card(self):
        result, *_ = _evaluated()
        payload = {
            "stance": "agree", "preferred_entry": "main", "confidence": 4,
            "read": "ok", "risks": [], "proposal": _good_proposal(result),
        }
        assert parse_ai_read(payload).proposal is None

    def test_a_bad_proposal_drops_only_itself(self):
        result, audit, cat = _catalog()
        payload = {
            "stance": "agree", "preferred_entry": "main", "confidence": 4,
            "read": "ok", "risks": [], "proposal": {"order": "limit", "entry": "x"},
        }
        read = parse_ai_read(payload, catalog=cat)
        assert read is not None and read.proposal is None and read.proposal_note


# ------------------------------------------------------------------ format


def _read_with(proposal):
    return AIRead(
        stance="agree", preferred_entry="main", confidence=4,
        read="Context agrees.", risks=[], model="stub", as_of="10:30",
        proposal=proposal,
    )


class TestFormat:
    def test_the_block_prints_the_order_rr_and_cancel_line(self):
        set_ai_floor(2.0)
        p = AIProposal(
            order="limit", direction="long", basis="m5_fvg", entry=3139.5,
            stop=3128.0, target=3221.0, invalidation="M5 close <below> 3128", rr=7.09,
        )
        text = format_ai_read(_read_with(p))
        assert "📐 AI setup: LIMIT LONG 3139.50 · SL 3128.00 · TP 3221.00 · 1:7.1 (M5 FVG)" in text
        assert "✖ cancel if: M5 close &lt;below&gt; 3128" in text
        assert "below the" not in text

    def test_below_floor_is_flagged(self):
        set_ai_floor(2.0)
        p = AIProposal(
            order="market", direction="short", basis="market", entry=3150.0,
            stop=3160.0, target=3140.0, rr=1.0, below_floor=True,
        )
        text = format_ai_read(_read_with(p))
        assert "MARKET SHORT 3150.00" in text
        assert "⚠️ below the 1:2 floor — wait" in text

    def test_none_prints_as_wait(self):
        p = AIProposal(order="none", direction="none", basis="none", invalidation="sweep first")
        assert "📐 AI setup: none — wait · sweep first" in format_ai_read(_read_with(p))

    def test_no_proposal_leaves_the_block_as_before(self):
        assert "📐" not in format_ai_read(_read_with(None))

    def test_russian(self):
        i18n.set_language("ru")
        try:
            p = AIProposal(
                order="limit", direction="long", basis="h1_zone", entry=3139.5,
                stop=3128.0, target=3221.0, rr=7.09, below_floor=True,
            )
            text = format_ai_read(_read_with(p))
            assert "📐 Сетап от AI: LIMIT LONG 3139.50" in text and "(зона H1)" in text
            assert "ниже порога 1:2 — ждать" in text
        finally:
            i18n.set_language("en")


# ------------------------------------------------------------ limit-first


class TestLimitFirstCard:
    def test_a_stale_setup_prints_a_limit_at_the_rung(self):
        result, *_ = _evaluated()
        s = result.setup
        text = format_result(result)
        assert "Enter at market" not in text
        assert f"⏳ Limit order at M5 FVG   {s.entry:.2f}   ← SL {s.stop_loss:.2f}" in text
        assert f"price {result.price:.2f} has run {s.entry_gap_r:.1f}R past it" in text
        assert "🎯 TP1" in text

    def test_a_fresh_setup_still_enters_at_market(self):
        result, *_ = _evaluated(max_gap_r=99.0)
        text = format_result(result)
        assert "Enter at market" in text and "⏳ Limit order" not in text

    def test_the_audit_names_the_same_order(self):
        result, h4, h1, m5 = _evaluated()
        audit = build_pending(result, ETH, h4, h1, m5)
        text = format_setup_analysis("ETHUSD", result, audit, ETH, as_of="10:30")
        s = result.setup
        assert "🚨 <b>Setup formed</b>" in text
        assert f"⏳ Limit order at M5 FVG   {s.entry:.2f}" in text
        assert "— market entry" not in text

    def test_the_journal_already_tracks_the_rung(self):
        # `record` stores setup.entry as a pending order — the limit the
        # card now names is the price the journal was always tracking
        from app.services.smc.db import Database
        from app.services.smc.journal import SignalJournal
        import tempfile
        import os

        result, *_ = _evaluated()
        with tempfile.TemporaryDirectory() as d:
            journal = SignalJournal(Database(os.path.join(d, "j.db")))
            row = journal.record(result)
        assert row["entry"] == result.setup.entry and row["status"] == "pending"


# ------------------------------------------------------------------- chart


class TestChart:
    def test_setup_chart_draws_the_box(self):
        result, audit, cat = _catalog()
        p, _ = validate_proposal(_good_proposal(result), cat)
        plain = render_setup_chart(result)
        with_box = render_setup_chart(result, proposal=p)
        assert with_box[:4] == b"\x89PNG" and with_box != plain

    def test_setup_chart_ignores_a_none_proposal(self):
        result, *_ = _evaluated()
        p = AIProposal(order="none", direction="none", basis="none")
        assert render_setup_chart(result, proposal=p) == render_setup_chart(result)

    def test_plan_chart_draws_the_box_even_without_scenarios(self):
        result, h4, h1, m5 = _evaluated()
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        p = AIProposal(
            order="limit", direction="long", basis="h1_zone", entry=3135.0,
            stop=3128.0, target=3221.0, rr=12.0,
        )
        png = render_plan_chart(plan, h1, proposal=p)
        assert png is not None and png[:4] == b"\x89PNG"
        plan.scenarios = []
        assert render_plan_chart(plan, h1) is None
        assert render_plan_chart(plan, h1, proposal=p) is not None


# ---------------------------------------------------------------- planbook


class TestPlanbook:
    def _entry(self):
        result, h4, h1, m5 = _evaluated()
        audit = build_pending(result, ETH, h4, h1, m5)
        cat = build_catalog(result, ETH, audit)
        p, _ = validate_proposal(_good_proposal(result), cat)
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        entry = PlanEntry(
            plan=plan, data={"h4": h4, "h1": h1, "m5": m5}, as_of="10:30",
            result=result, audit=audit, ai_read=_read_with(p),
        )
        return result, entry, p

    def test_snapshot_stores_the_proposal_and_the_match_reads_it_back(self):
        result, entry, p = self._entry()
        snap = primary_plan_snapshot(entry, "2026-09-13")
        assert snap["ai"]["proposal"]["entry"] == p.entry
        m = match_primary_plan(snap, result)
        assert m is not None and m.ai_proposal["order"] == "limit"
        text = describe_for_ai(result, ETH, plan=m, candles=False)
        assert f"Your proposed order then: limit LONG at {p.entry:.2f}" in text


# ----------------------------------------------------------------- watcher


class _Notifier:
    def __init__(self):
        self.sent, self.edited, self.photos, self.photo_edits = [], [], [], []

    async def send(self, text, reply_markup=None, disable_notification=False):
        self.sent.append(text)
        return len(self.sent)

    async def edit_message(self, message_id, text, reply_markup=None):
        self.edited.append((message_id, text))
        return True

    async def send_photo(self, photo, caption=None, reply_to=None):
        self.photos.append(photo)
        return 99

    async def edit_photo(self, message_id, photo):
        self.photo_edits.append((message_id, photo))
        return True

    async def pin(self, message_id):
        pass


class TestWatcher:
    @pytest.mark.asyncio
    async def test_the_alert_read_gets_a_catalog_and_redraws_the_chart(self, tmp_path):
        result, audit, cat = _catalog()
        p, _ = validate_proposal(_good_proposal(result), cat)
        reader = _StubReader(read=_read_with(p))
        w = _watcher(tmp_path, reader)
        w.notifier = _Notifier()
        w._chart_messages = {1: 99}  # the stubbed _send_chart never records one
        assert await w._send_alert("ETHUSD", result, "fp") is True

        facts, images, _ = reader.facts[0]
        assert "ALLOWED ENTRY BANDS" in facts and "Recent M5 candles" in facts
        assert isinstance(reader.catalogs[0], LevelCatalog)
        assert "📐 AI setup: LIMIT LONG" in w.notifier.edited[-1][1]
        assert w.notifier.photo_edits and w.notifier.photo_edits[0][0] == 99
        assert w.notifier.photo_edits[0][1][:4] == b"\x89PNG"

    @pytest.mark.asyncio
    async def test_no_chart_message_means_no_redraw(self, tmp_path):
        result, audit, cat = _catalog()
        p, _ = validate_proposal(_good_proposal(result), cat)
        w = _watcher(tmp_path, _StubReader(read=_read_with(p)))
        w.notifier = _Notifier()
        assert await w._send_alert("ETHUSD", result, "fp") is True
        assert w.notifier.photo_edits == []
        assert "📐" in w.notifier.edited[-1][1]

    def test_the_news_line(self, tmp_path):
        from datetime import timedelta

        from app.services.smc.news import NewsEvent

        w = _watcher(tmp_path, _StubReader())
        assert w._next_news_line("ETHUSD") is None

        class _Cal:
            def upcoming(self, currencies, within, now=None):
                assert currencies == {"USD"}
                return [NewsEvent(time=now + timedelta(minutes=95), currency="USD", title="CPI")]

        w.news = _Cal()
        line = w._next_news_line("ETHUSD")
        assert line.startswith("Next red news: USD CPI at ") and "(in 9" in line

    @pytest.mark.asyncio
    async def test_plan_sends_the_m5_chart_with_the_box_once_a_setup_formed(self, tmp_path):
        result, entry, p = TestPlanbook()._entry()
        w = _watcher(tmp_path, _StubReader(read=entry.ai_read))
        w.notifier = _Notifier()
        w.planbook = PlanBook()
        w.planbook.update("ETHUSD", entry)
        await w._send_setup_analysis("ETHUSD", fresh=False)
        assert len(w.notifier.photos) == 2  # H1 plan chart + M5 setup chart
        assert "📐 AI setup" in w.notifier.sent[0]
        assert asyncio.iscoroutinefunction(w._m5_chart_png)


class TestChartMarks:
    """Owner request 2026-09-13: the AI order in a readable top-left box,
    the M5 FVG / OB outlined and named on both charts."""

    def test_plan_chart_draws_the_setup_bands(self):
        result, h4, h1, m5 = _evaluated()
        plan = build_plan(ETH, h4, h1, m5, min_rr=1.0)
        plain = render_plan_chart(plan, h1)
        marked = render_plan_chart(plan, h1, setup=result.setup)
        assert marked[:4] == b"\x89PNG" and marked != plain

    def test_setup_chart_survives_a_rejected_imbalance_and_no_order_block(self):
        result, *_ = _evaluated()
        s = result.setup
        s.rejected_fvg, s.fvg, s.order_block = s.fvg, None, None
        assert render_setup_chart(result)[:4] == b"\x89PNG"


# ---------------------------------------------------------- shadow orders


def _shadow_journal(tmp_path):
    from app.services.smc.db import Database
    from app.services.smc.journal import SignalJournal

    return SignalJournal(Database(str(tmp_path / "j.db")))


def candle(ts, open_, high, low, close):
    from app.services.smc.models import Candle

    return Candle(timestamp=ts, open=open_, high=high, low=low, close=close)


def _limit(entry=3139.5, stop=3128.0, target=3221.0, order="limit", direction="long"):
    return AIProposal(
        order=order, direction=direction, basis="m5_fvg", entry=entry, stop=stop,
        target=target, rr=round(abs(target - entry) / abs(entry - stop), 2),
    )


class TestShadowOrders:
    """D28 follow-up (owner pick 2026-09-13): Claude's proposed order is a
    shadow journal row — tracked like a setup, invisible to /stats,
    discipline and Rule 0.4."""

    def test_a_limit_is_recorded_pending_with_rule_10_expiry(self, tmp_path):
        j = _shadow_journal(tmp_path)
        row = j.record_ai("ETHUSD", _limit(), T0, "Frankfurt/London", tolerance=2.0)
        assert row["origin"] == "ai" and row["status"] == "pending"
        assert row["tier"] == "ai" and row["message_id"] is None and row["taken"] is None
        assert row["expires_at"] is not None and row["profile_key"] == "alert"
        # persisted through the origin column
        assert j.db.signals_all()[0]["origin"] == "ai"

    def test_market_is_open_at_once_and_none_records_nothing(self, tmp_path):
        j = _shadow_journal(tmp_path)
        assert j.record_ai("ETHUSD", _limit(order="market"), T0, "NY")["status"] == "open"
        none = AIProposal(order="none", direction="none", basis="none")
        assert j.record_ai("ETHUSD", none, T0, "NY") is None

    def test_the_same_order_twice_is_one_row(self, tmp_path):
        j = _shadow_journal(tmp_path)
        assert j.record_ai("ETHUSD", _limit(), T0, "NY", tolerance=2.0) is not None
        assert j.record_ai("ETHUSD", _limit(entry=3140.5), T0, "NY", tolerance=2.0) is None
        assert j.record_ai("ETHUSD", _limit(entry=3100.0, stop=3090.0), T0, "NY", tolerance=2.0)
        assert len(j.signals) == 2

    def test_tracking_fills_and_resolves_like_a_setup(self, tmp_path):
        j = _shadow_journal(tmp_path)
        j.record_ai("ETHUSD", _limit(), T0, "NY")
        candles = [
            candle(T0.replace(minute=35), 3150, 3152, 3139, 3145),  # touch -> open
            candle(T0.replace(minute=40), 3145, 3222, 3144, 3220),  # target
        ]
        events = j.update_pair("ETHUSD", candles)
        assert [e for _, e in events] == ["filled", "tp"]
        assert j.signals[0]["status"] == "tp"
        assert "AI setups" in j.ai_setups_text()
        assert "1 wins / 0 stops (100%) · realized +7.1R" in j.ai_setups_text()
        # the engine's own /stats never sees the shadow row
        assert "Journal is empty" in j.stats_text()

    def test_the_target_taken_before_the_fill_cancels_the_order_once(self, tmp_path):
        j = _shadow_journal(tmp_path)
        j.record_ai("ETHUSD", _limit(), T0, "NY")
        before = [candle(T0.replace(minute=25), 3150, 3225, 3149, 3200)]  # before the order
        near = [candle(T0.replace(minute=35), 3150, 3215, 3149, 3210)]  # short of the target
        assert j.cancel_ai_orders("ETHUSD", before + near) == []
        taken = [candle(T0.replace(minute=40), 3210, 3222, 3209, 3218)]  # target swept, no fill
        cancelled = j.cancel_ai_orders("ETHUSD", before + near + taken)
        assert len(cancelled) == 1
        signal, _, reason = cancelled[0]
        assert signal["status"] == "cancelled" and reason == "target_taken"
        assert j.cancel_ai_orders("ETHUSD", before + near + taken) == []
        assert "1 cancelled" in j.ai_setups_text()

    def test_a_gap_over_the_stop_is_a_fill_then_a_stop_not_a_cancel(self, tmp_path):
        j = _shadow_journal(tmp_path)
        j.record_ai("ETHUSD", _limit(direction="short", entry=3160.0, stop=3170.0, target=3100.0), T0, "NY")
        gap = [candle(T0.replace(minute=35), 3180, 3185, 3175, 3182)]  # opened above the stop
        assert j.cancel_ai_orders("ETHUSD", gap) == []
        j.update_pair("ETHUSD", gap)
        assert j.signals[0]["status"] == "sl"

    def test_a_touch_before_the_close_through_is_a_fill_not_a_cancel(self, tmp_path):
        j = _shadow_journal(tmp_path)
        j.record_ai("ETHUSD", _limit(), T0, "NY")
        candles = [
            candle(T0.replace(minute=35), 3150, 3151, 3139, 3141),  # touches the entry
            candle(T0.replace(minute=40), 3141, 3142, 3120, 3125),  # closes through the stop
        ]
        assert j.cancel_ai_orders("ETHUSD", candles) == []
        j.update_pair("ETHUSD", candles)
        assert j.signals[0]["status"] == "sl"  # filled, then stopped — a real loss


class TestShadowOrdersInTheWatcher:
    @pytest.mark.asyncio
    async def test_the_alert_read_records_the_order(self, tmp_path):
        result, audit, cat = _catalog()
        p, _ = validate_proposal(_good_proposal(result), cat)
        w = _watcher(tmp_path, _StubReader(read=_read_with(p)))
        w.notifier = _Notifier()
        assert await w._send_alert("ETHUSD", result, "fp") is True
        rows = [s for s in w.journal.signals if s.get("origin") == "ai"]
        assert len(rows) == 1 and rows[0]["entry"] == p.entry
        assert rows[0]["profile_key"] == "alert"
        engine_rows = [s for s in w.journal.signals if s.get("origin") != "ai"]
        assert len(engine_rows) == 1

    @pytest.mark.asyncio
    async def test_the_cycle_cancels_and_says_so_once(self, tmp_path):
        w = _watcher(tmp_path, _StubReader())
        w.notifier = _Notifier()
        w.journal.record_ai("ETHUSD", _limit(), T0, "NY")
        result, *_ = _evaluated()
        result.m5_candles = [candle(T0.replace(minute=40), 3210, 3222, 3209, 3218)]
        await w._maybe_ai_order_cancelled("ETHUSD", result)
        await w._maybe_ai_order_cancelled("ETHUSD", result)
        assert len(w.notifier.sent) == 1
        assert "📐 <b>ETHUSD: AI order cancelled</b>" in w.notifier.sent[0]
        assert "limit at 3139.50 never filled and its target 3221.00" in w.notifier.sent[0]

    def test_russian_cancel_message(self):
        i18n.set_language("ru")
        try:
            text = i18n.t(
                "📐 <b>{pair}: AI order cancelled</b> — the {side} limit at {entry} never "
                "filled and its target {tp} was already taken ({hhmm} Prague): the move "
                "played out without an entry. Pull the limit if you placed it.",
                pair="ETHUSD", side="SHORT", entry="2520.40", tp="2436.39", hhmm="15:40",
            )
            assert text.startswith("📐 <b>ETHUSD: ордер AI снят</b>")
        finally:
            i18n.set_language("en")
