"""Audit polish (owner picks, 2026-09-10): Rule 8 size per rung, the session
clock, TP dedup within the instrument tolerance, stacked chart labels, and
Claude's stance scored against outcomes in /journal."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc import i18n
from app.services.smc.ai_read import describe_for_ai
from app.services.smc.chart import _stacked_y
from app.services.smc.db import Database
from app.services.smc.engine import position_size
from app.services.smc.instruments import get_instrument
from app.services.smc.journal import SignalJournal
from app.services.smc.liquidity import LiquidityLevel, liquidity_ladder, take_profits
from app.services.smc.models import Direction
from app.services.smc.notifier import format_setup_analysis, session_time_left
from app.services.smc.pending import build_pending
from tests.test_smc.test_autoplan import TestStrategyAuditButton
from tests.test_smc.test_visuals import _approved_result


def _lv(price, tf="H1", n=1, is_high=True):
    return LiquidityLevel(price=price, is_high=is_high, timeframe=tf, equal_count=n)


class TestTakeProfitDedup:
    def test_pools_within_tolerance_collapse_to_the_richer_one(self):
        levels = [_lv(2482.76), _lv(2519.48, "H1"), _lv(2521.30, "H4"), _lv(2600.0)]
        rungs = liquidity_ladder(levels, Direction.LONG, 2396.83, tolerance=2.0)
        assert [r.price for r in rungs] == [2482.76, 2521.30, 2600.0]  # H4 outranks H1
        tps = take_profits(levels, Direction.LONG, 2396.83, 2388.0, 2.0, tolerance=2.0)
        assert [round(tp.price, 2) for tp in tps] == [2480.76, 2519.30, 2598.0]

    def test_zero_tolerance_keeps_distinct_prices(self):
        levels = [_lv(2519.48), _lv(2521.30, "H4")]
        rungs = liquidity_ladder(levels, Direction.LONG, 2396.83)
        assert [r.price for r in rungs] == [2519.48, 2521.30]

    def test_exact_duplicates_still_merge_without_tolerance(self):
        levels = [_lv(2500.0, "H1"), _lv(2500.0, "H4", n=2)]
        rungs = liquidity_ladder(levels, Direction.LONG, 2400.0)
        assert len(rungs) == 1 and rungs[0].equal_count == 2

    def test_short_side_and_the_audit_use_the_instrument_tolerance(self):
        levels = [_lv(2300.0, is_high=False), _lv(2298.5, "H4", is_high=False)]
        rungs = liquidity_ladder(levels, Direction.SHORT, 2400.0, tolerance=2.0)
        assert [r.price for r in rungs] == [2298.5]
        entry = TestStrategyAuditButton()._audited_entry()
        audit = build_pending(
            entry.result, get_instrument("ETHUSD"),
            entry.data["h4"], entry.data["h1"], entry.data["m5"],
        )
        for e in audit.entries:
            prices = [tp.price for tp in e.targets]
            assert all(b - a > 2.0 for a, b in zip(prices, prices[1:]))


class TestPositionSize:
    def test_crypto_and_forex_sizes(self):
        eth = get_instrument("ETHUSD")
        assert position_size(eth, 2396.83, 8.83, 1000.0, 2.0, compact=True) == "2.2650 ETH"
        assert "risk $20.00 = 2.0% of $1000 deposit" in position_size(eth, 2396.83, 8.83, 1000.0, 2.0)
        jpy = get_instrument("USDJPY")
        size = position_size(jpy, 150.0, 0.20, 1000.0, 2.0, compact=True)
        assert size.endswith("lots") and float(size.split()[0]) > 0
        assert position_size(eth, 2396.83, 8.83, None, 2.0) is None
        assert position_size(eth, 2396.83, 0.0, 1000.0, 2.0) is None

    def test_audit_table_carries_a_size_row_only_with_a_deposit(self):
        i18n.set_language("en")
        entry = TestStrategyAuditButton()._audited_entry()
        inst = get_instrument("ETHUSD")
        without = format_setup_analysis("ETHUSD", entry.result, entry.audit, inst)
        assert "Size" not in without
        with_dep = format_setup_analysis(
            "ETHUSD", entry.result, entry.audit, inst, deposit=1000.0, risk_pct=2.0,
        )
        assert "\nSize " in with_dep and " ETH" in with_dep


class TestSessionClock:
    def _result_at(self, hh, mm):
        from app.services.smc.sessions import PRAGUE

        r = _approved_result()
        local = PRAGUE.localize(datetime(2026, 9, 10, hh, mm))
        r.checked_at = local.astimezone(timezone.utc)
        r.session_name = "New York"
        return r

    def test_minutes_left_and_the_line(self):
        i18n.set_language("en")
        r = self._result_at(17, 25)
        minutes, end = session_time_left(r)
        assert minutes == 65
        text = format_setup_analysis(
            "ETHUSD", r, TestStrategyAuditButton()._audited_entry().audit,
            get_instrument("ETHUSD"),
        )
        assert "⏱ New York ends in 1h05 (18:30 Prague)" in text
        facts = describe_for_ai(r, get_instrument("ETHUSD"))
        assert "Session ends at 18:30 Prague (65 min left)" in facts

    def test_off_session_has_no_clock(self):
        r = _approved_result()
        r.session_name = None
        assert session_time_left(r) is None
        assert "Session ends" not in describe_for_ai(r, get_instrument("ETHUSD"))

    def test_russian_line(self):
        i18n.set_language("ru")
        r = self._result_at(17, 25)
        text = format_setup_analysis(
            "ETHUSD", r, TestStrategyAuditButton()._audited_entry().audit,
            get_instrument("ETHUSD"),
        )
        assert "⏱ Сессия New York закончится через 1ч05 (18:30 Прага)" in text


class TestStackedLabels:
    def test_close_labels_are_pushed_apart(self):
        placed = []
        y1 = _stacked_y(2396.83, placed, (2380.0, 2540.0))
        y2 = _stacked_y(2392.0, placed, (2380.0, 2540.0))  # 3% of the axis apart
        assert y1 == 2396.83
        assert y2 < 2392.0 and y1 - y2 >= 160 * 0.05 - 1e-9

    def test_far_labels_stay_on_price(self):
        placed = []
        assert _stacked_y(2400.0, placed, (2380.0, 2540.0)) == 2400.0
        assert _stacked_y(2482.76, placed, (2380.0, 2540.0)) == 2482.76

    def test_three_way_pile_up_resolves(self):
        placed = []
        ys = [_stacked_y(y, placed, (0.0, 100.0)) for y in (50.0, 50.5, 51.0, 49.5)]
        for a in ys:
            for b in ys:
                if a is not b:
                    assert a == b or abs(a - b) >= 5.0 - 1e-9

    def test_plan_chart_still_renders(self):
        from app.services.smc.chart import render_plan_chart

        entry = TestStrategyAuditButton()._audited_entry()
        png = render_plan_chart(entry.plan, entry.data["h1"])
        assert png is None or png[:8] == b"\x89PNG\r\n\x1a\n"


class TestClaudeAccuracy:
    def _journal(self, tmp_path):
        return SignalJournal(Database(str(tmp_path / "smc.db")))

    def _seed(self, journal, stance, status, taken=1):
        import uuid

        sig = {
            "id": uuid.uuid4().hex[:10], "pair": "ETHUSD", "direction": "long",
            "entry": 1.0, "stop_loss": 0.9, "take_profit": 1.2, "rr": 2.0,
            "session": "New York",
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "expires_at": None, "status": status, "taken": taken,
            "ai_stance": stance, "ai_confidence": 4,
        }
        journal.signals.append(sig)
        journal._persist(sig)
        return sig

    def test_attach_and_persist(self, tmp_path):
        journal = self._journal(tmp_path)
        sig = self._seed(journal, None, "pending")
        journal.attach_ai_read(sig["id"], "agree", 4)
        reloaded = SignalJournal(Database(str(tmp_path / "smc.db")))
        row = reloaded.get(sig["id"])
        assert row["ai_stance"] == "agree" and row["ai_confidence"] == 4

    def test_accuracy_text(self, tmp_path):
        i18n.set_language("en")
        journal = self._journal(tmp_path)
        for stance, status in (
            ("agree", "tp"), ("agree", "tp1_be"), ("agree", "sl"),
            ("caution", "sl"), ("against", "expired"), (None, "tp"),
        ):
            self._seed(journal, stance, status)
        text = journal.ai_accuracy_text()
        assert "Claude vs outcomes — last 90 days" in text
        assert "agree: 3 · 2 wins / 1 stops (67%)" in text
        assert "caution: 1 · 0 wins / 1 stops (0%)" in text
        assert "against" not in text  # expired never traded

    def test_empty(self, tmp_path):
        i18n.set_language("en")
        assert "no resolved alerts with a read yet" in self._journal(tmp_path).ai_accuracy_text()

    @pytest.mark.asyncio
    async def test_journal_command_appends_it(self):
        from app.services.smc.telegram_bot import TelegramCommandBot

        class _TJ:
            api_key = "k"

            def stats_text(self):
                return "trades"

        async def run_cycle():
            return "ok"

        bot = TelegramCommandBot(
            bot_token="123:dummy", owner_chat_id="1", state=None,
            run_cycle=run_cycle, status_text=lambda: "s",
            ai_stats_text=lambda: "🧠 acc", trade_journal=_TJ(),
        )
        sent = []

        async def _api(method, http_timeout=35.0, **payload):
            sent.append(payload)
            return {"ok": True}

        bot._api = _api
        await bot._handle_command("/journal")
        assert sent[-1]["text"] == "trades\n\n🧠 acc"


class TestAuditCarriesTheEngineWarnings:
    """2026-09-10 (owner screenshot): the audit announced «Сетап
    сформирован — вход по рынку 2441.54 · риск $45.22 · TP1 1:0.1» with no
    hint that price had run 2.5R past the imbalance. The 🚨 card had always
    carried that warning; the audit — the screen the owner plans from —
    had not."""

    def _audited(self, warnings=(), funding=None):
        """A formed setup: the audit's market branch needs a market rung,
        which the waiting fixture (price not in the zone yet) has none of."""
        from app.services.smc.pending import ROLE_MARKET, PendingEntry

        entry = TestStrategyAuditButton()._audited_entry()
        entry.result.warnings = list(warnings)
        entry.result.funding_warning = funding
        entry.audit.market = PendingEntry(
            role=ROLE_MARKET, label="market", direction=entry.audit.direction,
            entry=3160.0, stop_loss=3128.0,
        )
        return entry

    def test_warnings_follow_the_market_line(self):
        i18n.set_language("en")
        entry = self._audited(["price has run 2.5R past the imbalance"])
        text = format_setup_analysis(
            "ETHUSD", entry.result, entry.audit, get_instrument("ETHUSD"),
        )
        assert "🚨 <b>Setup formed</b>" in text
        assert "⚠️ price has run 2.5R past the imbalance" in text
        market_at = text.index("Setup formed")
        table_at = text.index("Pending (limit) entries")
        assert market_at < text.index("2.5R past") < table_at  # between the two

    def test_funding_warning_too(self):
        i18n.set_language("en")
        entry = self._audited(funding="Funding 0.200%/8h is above the 0.10% level")
        text = format_setup_analysis(
            "ETHUSD", entry.result, entry.audit, get_instrument("ETHUSD"),
        )
        assert "⚠️ Funding 0.200%/8h is above the 0.10% level" in text

    def test_russian_warnings_are_translated(self):
        i18n.set_language("ru")
        from app.services.smc.i18n import t

        entry = self._audited([t("price has run {r}R past the imbalance", r="2.5")])
        text = format_setup_analysis(
            "ETHUSD", entry.result, entry.audit, get_instrument("ETHUSD"),
        )
        assert "⚠️ цена ушла на 2.5R от имбаланса" in text

    def test_a_clean_setup_adds_no_warning_line(self):
        i18n.set_language("en")
        entry = self._audited()
        text = format_setup_analysis(
            "ETHUSD", entry.result, entry.audit, get_instrument("ETHUSD"),
        )
        assert "⚠️ " not in text.split("Pending (limit) entries")[0]

    def test_the_card_and_the_audit_use_one_builder(self):
        from app.services.smc.notifier import _warning_lines

        entry = self._audited(["a", "b"], funding="c")
        assert _warning_lines(entry.result) == ["⚠️ a", "⚠️ b", "⚠️ c"]

    def test_html_in_a_warning_is_escaped(self):
        i18n.set_language("en")
        entry = self._audited(["fill < 50% of the gap"])
        text = format_setup_analysis(
            "ETHUSD", entry.result, entry.audit, get_instrument("ETHUSD"),
        )
        assert "fill &lt; 50%" in text and "fill < 50%" not in text


class TestLocalizedDuration:
    def test_hour_marker_follows_the_language(self):
        from app.services.smc.notifier import format_duration

        i18n.set_language("en")
        assert format_duration(65) == "1h05"
        assert format_duration(26) == "0h26"
        i18n.set_language("ru")
        assert format_duration(65) == "1ч05"
        assert format_duration(0) == "0ч00"
        assert format_duration(-5) == "0ч00"
