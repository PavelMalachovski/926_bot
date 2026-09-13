"""The cards and notices: what the owner reads, HTML-safe."""

from datetime import timedelta

from app.services.opus.decision import downgrade, parse_decision
from app.services.opus.messages import (
    budget_reason, format_ai_failure, format_event_without_read, format_expired,
    format_journal, format_plan_card, format_status, news_warning_line,
)
from app.services.opus.plan import (
    EXPIRED, OPEN, TP, advance_plan, plan_from_decision, trade_from_plan,
)
from app.services.smc.instruments import get_instrument
from app.services.smc.news import NewsEvent
from tests.test_opus.helpers import GOOD_LIMIT, GOOD_WAIT, NOW

JPY = get_instrument("USDJPY")


def plan(payload, **kw):
    return plan_from_decision(
        "USDJPY", parse_decision({**payload, **kw}), NOW, 147.25,
        NOW + timedelta(hours=3, minutes=30), "09.09 10:30",
    )


class TestCard:
    def test_limit_card(self):
        text = format_plan_card(plan(GOOD_LIMIT), JPY)
        assert "🧠 <b>Opus · USDJPY</b> · 09.09 10:30 Prague · price 147.250" in text
        assert "📊 Bias: SHORT · confidence 4/5" in text
        assert "📍 <b>LIMIT SHORT @ 148.100</b>" in text
        assert "🛑 SL 148.400 (risk 30.0 pips)" in text
        assert "🎯 TP1 147.400 (RR 1:2.3) · TP2 147.000 (1:3.7)" in text
        assert "❌ Cancel if M5 closes below 146.900" in text
        assert "⏳ Valid until 14:00 Prague (block end)" in text
        assert "<b>Why:</b>\n• H4 lower highs" in text and "<b>Risks:</b>\n• CPI at 14:30" in text
        assert "🔁" not in text

    def test_market_card(self):
        text = format_plan_card(plan(GOOD_LIMIT, action="market", entry=147.25,
                                     stop_loss=147.55, tp1=146.60, tp2=None,
                                     invalidation=None), JPY)
        assert "📈 <b>ENTER AT MARKET SHORT @ 147.250</b>" in text
        assert "TP2" not in text and "⏳" not in text and "❌" not in text

    def test_wait_card_with_zone(self):
        text = format_plan_card(plan(GOOD_WAIT), JPY)
        assert "⏸ <b>WAIT</b> — watch zone 146.900–147.050; Opus is asked again" in text
        assert "❌ Cancel if M5 closes below 146.800" in text
        assert "⏳ Valid until 14:00 Prague (day end)" in text

    def test_no_trade_card(self):
        text = format_plan_card(plan(GOOD_WAIT, action="no_trade", watch_low=None,
                                     watch_high=None, invalidation=None), JPY)
        assert "🚫 <b>NO TRADE</b>" in text and "watch zone" not in text

    def test_downgraded_card_shows_the_rejected_idea(self):
        d = downgrade(parse_decision(GOOD_LIMIT), ["RR to tp1 is 1:1.3, below <2>"])
        p = plan_from_decision("USDJPY", d, NOW, 147.25, None, "09.09 10:30")
        text = format_plan_card(p, JPY)
        assert "⏸ <b>WAIT</b>" in text
        assert "🛑 SL" not in text and "🎯 TP1" not in text  # the rejected idea's levels stay in the block below
        assert "⚠️ Opus wanted LIMIT SHORT @ 148.100 — rejected by the hard limits:" in text
        assert "   • RR to tp1 is 1:1.3, below &lt;2&gt;" in text

    def test_follow_up_names_the_event(self):
        p = plan(GOOD_LIMIT)
        p.trigger = "zone_reached"
        assert "🔁 Re-read after: price reached the watch zone" in format_plan_card(p, JPY)

    def test_model_text_is_escaped(self):
        text = format_plan_card(plan(GOOD_LIMIT, read="fill < 50% & go", risks=["a <b> tag"]), JPY)
        assert "fill &lt; 50% &amp; go" in text and "a &lt;b&gt; tag" in text

    def test_news_warning_inside_validity(self):
        p = plan(GOOD_LIMIT)
        cpi = NewsEvent(time=NOW + timedelta(hours=2), currency="USD", title="CPI <m/m>")
        line = news_warning_line([cpi], timedelta(minutes=60), timedelta(minutes=15), p)
        assert line.startswith("📰 12:30 USD CPI &lt;m/m&gt; — no entries 11:30–12:45")
        later = NewsEvent(time=NOW + timedelta(hours=7), currency="USD", title="x")
        assert news_warning_line([later], timedelta(minutes=60), timedelta(minutes=15), p) is None
        assert news_warning_line([cpi], timedelta(minutes=60), timedelta(minutes=15),
                                 plan(GOOD_LIMIT, action="market")) is None
        assert "pull the limit before" in format_plan_card(p, JPY, line)


class TestNotices:
    def test_expired(self):
        p = plan(GOOD_LIMIT)
        advance_plan(p, [], NOW + timedelta(hours=4))
        assert p.status == EXPIRED
        assert "⏳ <b>USDJPY</b>: the limit plan expired at 14:30 Prague — pull the order" in format_expired(p)

    def test_event_without_read(self):
        p = plan(GOOD_WAIT)
        text = format_event_without_read(p, "zone_reached", 147.0, JPY, budget_reason(6, 6))
        assert text == (
            "👀 <b>USDJPY</b>: price reached the watch zone 146.900–147.050 (147.000). "
            "Opus event calls for today are spent (6/6) — press /plan to ask Opus."
        )
        text = format_event_without_read(plan(GOOD_LIMIT), "invalidated", 146.8, JPY, "x")
        assert "M5 closed beyond the invalidation 146.900 (146.800) — pull the limit" in text

    def test_failure_is_escaped(self):
        assert "api: 401 &lt;bad&gt;" in format_ai_failure("USDJPY", "api: 401 <bad>")


class TestStatusJournal:
    def test_status(self):
        p = plan(GOOD_LIMIT)
        text = format_status(["USDJPY", "ETHUSD"], {"USDJPY": p}, [], {
            "USDJPY": JPY, "ETHUSD": get_instrument("ETHUSD"),
        })
        assert "USDJPY: limit pending · LIMIT SHORT @ 148.100 · 09.09 10:30" in text
        assert "ETHUSD: no plan yet — /plan" in text

    def test_journal(self):
        p = plan(GOOD_LIMIT)
        tr = trade_from_plan(p, NOW, "limit")
        tr.status, tr.result_r = TP, 2.33
        history = [{"pair": "USDJPY", "created_at": NOW.isoformat(), "action": "limit"},
                   {"pair": "USDJPY", "created_at": NOW.isoformat(), "action": "wait"}]
        text = format_journal([tr], history, days=30)
        assert "Decisions: 2 · limit 1 · market 0 · wait 1 · no trade 0" in text
        assert "Trades: 1 opened · 1 closed · 1 TP / 0 SL · +2.3R" in text
        assert "USDJPY: 1/1 · +2.3R" in text
        assert "nothing yet" in format_journal([], [], days=30)
