"""Premium/discount: the range, the position in it, and the PD radar.

Audit finding F3 (2026-08-26): the ⭐'s `pd` condition was measured on
whatever box the last two confirmed H1 pivots made — a five-to-fifteen-candle
leg on a timeframe the bias does not come from — and the answer never left the
code as a number. These pin the explicit replacement (owner decision D17) and
the radar built on it (owner request 2026-08-26).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.smc import pd
from app.services.smc.models import (
    AnalysisResult,
    Candle,
    Direction,
    Trend,
    Verdict,
    Zone,
)
from app.services.smc.notifier import format_pd_alert
from app.services.smc.sessions import PRAGUE
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    make_candles,
)

# The shared fixtures, with the boxes they produce pinned here once:
#   H4 3119.0 – 3301.0   (span 182.0, equilibrium 3210.0)
#   H1 3131.0 – 3221.0   (span  90.0, equilibrium 3176.0)
H4_LOW, H4_HIGH = 3119.0, 3301.0
H1_LOW, H1_HIGH = 3131.0, 3221.0


def h4():
    return make_candles(H4_UPTREND_CLOSES, step_minutes=240)


def h1():
    return make_candles(H1_PULLBACK_CLOSES, step_minutes=60)


# --------------------------------------------------------------- the range


def test_dealing_range_reads_the_last_confirmed_swings():
    rng = pd.dealing_range(h4(), "H4")
    assert (rng.low, rng.high, rng.timeframe) == (H4_LOW, H4_HIGH, "H4")
    assert rng.span == pytest.approx(H4_HIGH - H4_LOW)
    assert rng.equilibrium == pytest.approx((H4_LOW + H4_HIGH) / 2)


def test_dealing_range_needs_both_sides():
    flat = make_candles([100.0] * 12, step_minutes=60)
    assert pd.dealing_range(flat, "H1") is None


def test_dealing_range_refuses_an_inverted_box():
    """A steady decline pairs an early swing low with a later, LOWER swing
    high. Both are real pivots; the pairing is degenerate, and no midpoint
    can be read off it."""
    def c(i, o, high, low, close):
        return Candle(
            timestamp=datetime(2026, 3, 2, tzinfo=timezone.utc)
            + timedelta(minutes=60 * i),
            open=o, high=high, low=low, close=close,
        )

    inverted = [
        c(0, 104, 105, 103, 104), c(1, 104, 104.5, 102, 103),
        c(2, 103, 103.5, 101, 102), c(3, 102, 102.5, 100.5, 101),
        c(4, 101, 101.5, 98.0, 99),      # swing low 98.0
        c(5, 99, 100, 98.5, 99.5), c(6, 99.5, 100, 99, 99.7),
        c(7, 99.7, 99.8, 95, 95.5), c(8, 95.5, 96, 93, 93.5),
        c(9, 93.5, 94, 91, 91.5),
        c(10, 91.5, 97.0, 91, 96.5),     # swing high 97.0, below the low
        c(11, 96.5, 96.8, 90, 91), c(12, 91, 92, 88, 89),
        c(13, 89, 90, 86, 87), c(14, 87, 88, 84, 85),
    ]
    assert pd.dealing_range(inverted, "H1") is None


def test_position_is_a_fraction_of_the_span():
    rng = pd.dealing_range(h4(), "H4")
    assert rng.position(H4_LOW) == pytest.approx(0.0)
    assert rng.position(H4_HIGH) == pytest.approx(1.0)
    assert rng.position(rng.equilibrium) == pytest.approx(0.5)


# ------------------------------------------------------------- which range


def test_resolve_range_prefers_h4_while_it_holds_price():
    rng = pd.resolve_range(h4(), h1(), 3200.0)
    assert (rng.timeframe, rng.low, rng.high) == ("H4", H4_LOW, H4_HIGH)


def test_resolve_range_falls_back_to_h1():
    """Price above the H4 box but inside the H1 one: H4 has nothing to say
    about a retracement it no longer contains, H1 does."""
    tight_h4 = make_candles(
        [3000, 3020, 3040, 3020, 3000, 3020, 3040, 3060, 3040, 3020, 3010],
        step_minutes=240,
    )
    assert pd.dealing_range(tight_h4, "H4").high < H1_HIGH
    rng = pd.resolve_range(tight_h4, h1(), 3200.0)
    assert rng.timeframe == "H1"


def test_resolve_range_is_none_when_price_left_every_box():
    """Expansion, not retracement. The ⭐ already treats an unreadable range
    as unmeasurable-and-passing, and this keeps that answer honest instead
    of extrapolating a percentage past the edge of the box."""
    assert pd.resolve_range(h4(), h1(), 9_000.0) is None


def test_resolve_range_basis_h1_asks_h1_alone():
    """`SMC_PD_BASIS=h1` restores the pre-audit reading without a deploy."""
    rng = pd.resolve_range(h4(), h1(), 3200.0, basis="h1")
    assert (rng.timeframe, rng.low, rng.high) == ("H1", H1_LOW, H1_HIGH)


# ------------------------------------------------------------- the reading


def test_side_labels_split_at_the_midpoint():
    rng = pd.dealing_range(h4(), "H4")
    assert pd.side_label(rng, H4_LOW + 0.2 * rng.span) == pd.DISCOUNT
    assert pd.side_label(rng, H4_LOW + 0.8 * rng.span) == pd.PREMIUM
    assert pd.side_label(rng, rng.equilibrium) == pd.EQUILIBRIUM


def test_ote_bands_are_the_62_to_79_percent_retracement():
    rng = pd.dealing_range(h4(), "H4")
    long_low, long_high = pd.ote(rng, Direction.LONG)
    short_low, short_high = pd.ote(rng, Direction.SHORT)
    # A long retraces DOWN from the high, a short UP from the low, so the two
    # bands are mirror images about the midpoint.
    assert rng.position(long_low) == pytest.approx(1 - pd.OTE_FAR)
    assert rng.position(long_high) == pytest.approx(1 - pd.OTE_NEAR)
    assert rng.position(short_low) == pytest.approx(pd.OTE_NEAR)
    assert rng.position(short_high) == pytest.approx(pd.OTE_FAR)
    assert long_high < rng.equilibrium < short_low


def test_favours_only_the_matching_side():
    assert pd.favours(Direction.LONG, pd.DISCOUNT) is True
    assert pd.favours(Direction.LONG, pd.PREMIUM) is False
    assert pd.favours(Direction.SHORT, pd.PREMIUM) is True
    assert pd.favours(Direction.SHORT, pd.DISCOUNT) is False
    # Equilibrium favours nobody: it is the midpoint, not a side.
    assert pd.favours(Direction.LONG, pd.EQUILIBRIUM) is False
    assert pd.favours(Direction.SHORT, pd.EQUILIBRIUM) is False


def test_read_reports_percent_side_and_ote_membership():
    rng = pd.dealing_range(h4(), "H4")
    inside = sum(pd.ote(rng, Direction.LONG)) / 2
    read = pd.read(h4(), h1(), inside, Direction.LONG)
    assert read.range.timeframe == "H4"
    assert read.label == pd.DISCOUNT
    assert read.favourable is True
    assert read.in_ote is True
    assert read.pct == int(round(read.position * 100))

    shallow = pd.read(h4(), h1(), rng.high - 0.1 * rng.span, Direction.LONG)
    assert shallow.label == pd.PREMIUM
    assert shallow.favourable is False
    assert shallow.in_ote is False


def test_read_is_none_outside_every_box():
    assert pd.read(h4(), h1(), 9_000.0, Direction.LONG) is None


# --------------------------------------------------------------- the radar


def _utc(hh, mm, day=26):
    return PRAGUE.localize(datetime(2026, 8, day, hh, mm)).astimezone(
        timezone.utc
    )


class _State:
    pd_already_pinged = WatcherState.pd_already_pinged
    remember_pd_ping = WatcherState.remember_pd_ping
    zone_muted_until = WatcherState.zone_muted_until

    def __init__(self):
        self.pd_pinged = {}
        self.zone_muted = {}
        self.pair_cooldown = {}

    def save(self):
        pass


class _Notifier:
    def __init__(self):
        self.sent = []
        self.fail_sends = False

    async def send(self, text, reply_markup=None, disable_notification=False):
        if self.fail_sends:
            return None
        self.sent.append((text, reply_markup))
        return len(self.sent)


def _watcher(monkeypatch):
    from smc_watcher import Watcher

    monkeypatch.setattr(settings.smc, "pd_alert", True)
    monkeypatch.setattr(settings.smc, "pd_basis", "h4")
    w = Watcher.__new__(Watcher)
    w.state = _State()
    w.notifier = _Notifier()
    return w


def _result(price, h4_trend=Trend.UP, h1_trend=Trend.UP, at=None, **kw):
    return AnalysisResult(
        symbol="ETHUSD",
        verdict=kw.pop("verdict", Verdict.WATCH),
        checked_at=at or _utc(10, 0),
        price=price,
        h4_trend=h4_trend,
        h1_trend=h1_trend,
        session_name=kw.pop("session_name", "Frankfurt/London"),
        price_decimals=2,
        h4_candles=h4(),
        h1_candles=h1(),
        m5_candles=make_candles([price] * 5),
        **kw,
    )


def _discount_price():
    rng = pd.dealing_range(h4(), "H4")
    return sum(pd.ote(rng, Direction.LONG)) / 2


def test_radar_announces_a_discount_under_a_long_bias(monkeypatch):
    w = _watcher(monkeypatch)
    asyncio.run(w._maybe_pd_alert("ETHUSD", _result(_discount_price())))
    assert len(w.notifier.sent) == 1
    text = w.notifier.sent[0][0]
    assert "DISCOUNT" in text and "bias LONG" in text
    assert "OTE" in text and "⭐" in text  # the price used is inside the OTE


def test_radar_fires_once_per_pair_per_block(monkeypatch):
    w = _watcher(monkeypatch)
    price = _discount_price()
    for minute in (0, 5, 10, 25):
        asyncio.run(
            w._maybe_pd_alert("ETHUSD", _result(price, at=_utc(10, minute)))
        )
    assert len(w.notifier.sent) == 1


def test_radar_re_arms_in_the_next_session_block(monkeypatch):
    w = _watcher(monkeypatch)
    price = _discount_price()
    asyncio.run(w._maybe_pd_alert("ETHUSD", _result(price, at=_utc(10, 0))))
    asyncio.run(
        w._maybe_pd_alert(
            "ETHUSD",
            _result(price, at=_utc(15, 0), session_name="New York"),
        )
    )
    assert len(w.notifier.sent) == 2


def test_radar_stays_silent_against_the_bias(monkeypatch):
    """A discount under a DOWN bias is the trend working, not an entry —
    pointing at it would be arguing for a counter-trend trade the strategy
    does not take (owner choice, 2026-08-26)."""
    w = _watcher(monkeypatch)
    asyncio.run(
        w._maybe_pd_alert(
            "ETHUSD",
            _result(_discount_price(), h4_trend=Trend.DOWN, h1_trend=Trend.DOWN),
        )
    )
    assert w.notifier.sent == []


def test_radar_takes_the_h1_bias_when_h4_is_flat(monkeypatch):
    """Rule 1's own precedence, not a second reading of the chart."""
    w = _watcher(monkeypatch)
    asyncio.run(
        w._maybe_pd_alert(
            "ETHUSD",
            _result(_discount_price(), h4_trend=Trend.FLAT, h1_trend=Trend.UP),
        )
    )
    assert len(w.notifier.sent) == 1


def test_radar_is_silent_when_both_timeframes_are_flat(monkeypatch):
    """The range state (D11): no trend to be early or late to."""
    w = _watcher(monkeypatch)
    asyncio.run(
        w._maybe_pd_alert(
            "ETHUSD",
            _result(
                _discount_price(), h4_trend=Trend.FLAT, h1_trend=Trend.FLAT
            ),
        )
    )
    assert w.notifier.sent == []


def test_radar_respects_the_pair_mute(monkeypatch):
    """🔕 covers the whole get-ready family — zone alerts and this."""
    w = _watcher(monkeypatch)
    w.state.zone_muted["ETHUSD"] = _utc(18, 0).isoformat()
    asyncio.run(w._maybe_pd_alert("ETHUSD", _result(_discount_price())))
    assert w.notifier.sent == []


def test_radar_is_silent_off_session(monkeypatch):
    w = _watcher(monkeypatch)
    asyncio.run(
        w._maybe_pd_alert(
            "ETHUSD", _result(_discount_price(), session_name=None)
        )
    )
    assert w.notifier.sent == []


def test_radar_is_silent_once_a_setup_has_formed(monkeypatch):
    """The 🚨 alert carries its own PD line — two messages, one moment."""
    w = _watcher(monkeypatch)
    asyncio.run(
        w._maybe_pd_alert(
            "ETHUSD",
            _result(_discount_price(), verdict=Verdict.APPROVED_LIMIT),
        )
    )
    assert w.notifier.sent == []


def test_a_failed_send_is_not_recorded(monkeypatch):
    """Same rule as every other alert here: mark AFTER delivery, so a
    message that never landed is retried on the next cycle."""
    w = _watcher(monkeypatch)
    w.notifier.fail_sends = True
    asyncio.run(w._maybe_pd_alert("ETHUSD", _result(_discount_price())))
    assert w.state.pd_pinged == {}
    w.notifier.fail_sends = False
    asyncio.run(w._maybe_pd_alert("ETHUSD", _result(_discount_price())))
    assert len(w.notifier.sent) == 1


def test_radar_is_silent_when_price_left_every_range(monkeypatch):
    w = _watcher(monkeypatch)
    asyncio.run(w._maybe_pd_alert("ETHUSD", _result(9_000.0)))
    assert w.notifier.sent == []


# -------------------------------------------------------------- the message


def test_pd_alert_message_names_the_range_the_price_and_the_ote():
    read = pd.read(h4(), h1(), _discount_price(), Direction.LONG)
    text = format_pd_alert("ETHUSD", read, 2)
    assert "H4 range" in text
    assert f"{read.range.low:.2f}" in text and f"{read.range.high:.2f}" in text
    assert f"← {read.pct}% of the range" in text
    assert "CHoCH + FVG" in text


def test_pd_alert_escapes_a_zone_it_names():
    read = pd.read(h4(), h1(), _discount_price(), Direction.LONG)
    zone = Zone(
        bottom=3131.0, top=3138.0, is_demand=True, pivot_index=0,
        timestamp=_utc(9, 0),
    )
    text = format_pd_alert("ETHUSD", read, 2, zone=zone)
    assert "H1 Demand zone" in text
    assert "<" not in text.replace("<b>", "").replace("</b>", "")


# ------------------------------------------------------- inside the engine


def test_engine_records_the_pd_read_and_measures_the_tier_on_it():
    """D17: the ⭐'s `pd` verdict is unchanged in shape — which half of the
    range the ENTRY sits in — but the range is now resolved explicitly and
    the number comes out with the result, so an alert can show it."""
    from tests.test_smc.helpers import m5_long_trigger
    from app.services.smc.engine import TripleSyncEngine
    from app.services.smc.sniper import pd_state

    engine = TripleSyncEngine(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0
    )
    result = engine.evaluate(
        h4=h4(), h1=h1(), m5=m5_long_trigger(),
        result=AnalysisResult(
            symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=_utc(10, 0),
            price_decimals=2,
        ),
    )
    assert result.verdict == Verdict.APPROVED_LIMIT
    assert result.pd is not None
    assert result.pd.range.timeframe == "H4"       # H4 first, and it holds
    assert result.pd.range.as_tuple() == (H4_LOW, H4_HIGH)
    assert result.pd.price == result.setup.entry   # the ENTRY, not the price
    # The tier reads exactly what the engine handed `sniper.pd_state`.
    expected = pd_state(
        Direction.LONG, result.setup.entry, result.pd.range.as_tuple()
    )
    assert ("pd" in result.setup.tier_missed) == (expected == "bad")


def test_engine_pd_basis_h1_restores_the_pre_audit_range():
    from tests.test_smc.helpers import m5_long_trigger
    from app.services.smc.engine import TripleSyncEngine

    engine = TripleSyncEngine(
        min_fvg_size=2.0, sl_buffer=2.0, min_rr=1.0, max_entry_gap_r=99.0,
        pd_basis="h1",
    )
    result = engine.evaluate(
        h4=h4(), h1=h1(), m5=m5_long_trigger(),
        result=AnalysisResult(
            symbol="ETHUSD", verdict=Verdict.SKIP, checked_at=_utc(10, 0),
            price_decimals=2,
        ),
    )
    assert result.pd.range.as_tuple() == (H1_LOW, H1_HIGH)


# ------------------------------------------------- the line on setup alerts


def _approved_result(pd_read):
    from app.services.smc.models import FVG, TradeSetup

    gap = FVG(
        top=3139.5, bottom=3135.0, index=4, is_bullish=True,
        timestamp=_utc(9, 30),
    )
    result = AnalysisResult(
        symbol="ETHUSD", verdict=Verdict.APPROVED_LIMIT,
        checked_at=_utc(10, 0), price=3150.0, h4_trend=Trend.UP,
        h1_trend=Trend.UP, price_decimals=2,
    )
    result.pd = pd_read
    result.setup = TradeSetup(
        direction=Direction.LONG, entry=3139.5, stop_loss=3128.0,
        take_profit=3219.0, rr=6.91, fvg=gap, tp1=3162.5, runner_tp=3174.0,
        tier_star=False, tier_missed=["pd"],
    )
    return result


def test_detector_alert_carries_the_pd_percentage():
    from app.services.smc.notifier import format_result

    read = pd.read(h4(), h1(), 3139.5, Direction.LONG)
    text = format_result(_approved_result(read))
    assert f"PD {read.pct}% {read.label}" in text
    assert "OTE" in text


def test_quiet_line_explains_a_missed_pd_with_a_number():
    """"Missed for ⭐: pd" is the most common verdict on this line and was
    the least actionable one — the number is what makes it a decision."""
    from app.services.smc.notifier import format_quiet_setup

    read = pd.read(h4(), h1(), 3139.5, Direction.LONG)
    text = format_quiet_setup(_approved_result(read))
    assert "Missed for ⭐: pd · PD " in text
    assert f"{read.pct}% {read.label} (H4)" in text


def test_alerts_say_nothing_about_pd_when_it_could_not_be_read():
    from app.services.smc.notifier import format_quiet_setup, format_result

    for text in (
        format_result(_approved_result(None)),
        format_quiet_setup(_approved_result(None)),
    ):
        assert "PD " not in text
