"""Triple Sync + Imbalance strategy engine (rules 0-8 orchestration)."""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import structlog

from app.services.smc.data import BinanceDataFetcher
from app.services.smc.fvg import best_rejected_fvg, select_valid_fvg
from app.services.smc.instruments import Instrument, get_instrument
from app.services.smc.liquidity import (
    find_liquidity,
    liquidity_ladder,
    nearest_liquidity,
)
from app.services.smc.models import (
    AnalysisResult,
    Candle,
    Direction,
    TradeSetup,
    Trend,
    Verdict,
)
from app.services.smc.profiles import CONSERVATIVE, StrategyProfile
from app.services.smc.sessions import active_session
from app.services.smc.structure import (
    detect_trend,
    find_choch,
    find_h1_zone,
    find_order_block,
    h4_choch_direction,
    sweep_extreme,
    zone_ladder,
    zone_touch_span,
)

logger = structlog.get_logger(__name__)

# Funding rate thresholds per 8h (Rule 9.3)
FUNDING_WARN = 0.0005  # 0.05%
FUNDING_DANGER = 0.001  # 0.1%


# A market whose newest M5 candle is older than this is considered closed
# (forex weekend); crypto trades 24/7 and never triggers it.
MARKET_STALE_AFTER = timedelta(minutes=30)


class TripleSyncEngine:
    """Runs one full strategy pass for a single instrument."""

    def __init__(
        self,
        instrument: Optional[Instrument] = None,
        min_fvg_size: Optional[float] = None,
        sl_buffer: Optional[float] = None,
        min_rr: float = 1.0,
        max_entry_gap_r: float = 0.75,
        risk_pct: float = 2.0,
        deposit: Optional[float] = None,
        enforce_sessions: bool = True,
        profile: Optional[StrategyProfile] = None,
        fetcher=None,
    ):
        self.instrument = instrument or get_instrument("ETHUSD")
        self.display_symbol = self.instrument.key
        self.profile = profile or CONSERVATIVE
        self.min_fvg_size = (
            min_fvg_size if min_fvg_size is not None else self.instrument.min_fvg
        )
        self._effective_min_fvg = self.min_fvg_size * self.profile.fvg_size_factor
        self.sl_buffer = (
            sl_buffer if sl_buffer is not None else self.instrument.sl_buffer
        )
        self.min_rr = min_rr
        self.max_entry_gap_r = max_entry_gap_r
        self.risk_pct = risk_pct
        self.deposit = deposit
        self.enforce_sessions = enforce_sessions
        self.fetcher = fetcher or BinanceDataFetcher(self.instrument.source_symbol)

    async def analyze(self) -> AnalysisResult:
        """Fetch fresh data and evaluate the full checklist."""
        now = datetime.now(tz=timezone.utc)
        result = AnalysisResult(
            symbol=self.display_symbol,
            verdict=Verdict.SKIP,
            checked_at=now,
            price_decimals=self.instrument.price_decimals,
        )

        # Rule 0.1 — session filter: 08:00-18:30 Prague; forex only Mon-Fri,
        # crypto every day
        result.session_name = active_session(
            now, require_weekday=self.instrument.source == "forex"
        )
        if self.enforce_sessions and result.session_name is None:
            result.verdict = Verdict.OFF_SESSION
            result.reasons.append(
                f"Outside trading hours (08:00-18:30 Prague"
                f"{', Mon-Fri' if self.instrument.source == 'forex' else ''}) "
                f"— no entries for {self.display_symbol}"
            )
            return result

        data = await self.fetcher.fetch_all_timeframes()
        result.price = data["m5"][-1].close
        result.m5_candles = data["m5"]  # kept for chart rendering
        result.h4_candles = data["h4"]  # planbook recompute reads these —
        result.h1_candles = data["h1"]  # zero extra fetches per cycle

        # Closed market (forex weekend): the newest M5 candle is stale.
        age = now - data["m5"][-1].timestamp
        if age > MARKET_STALE_AFTER:
            result.verdict = Verdict.OFF_SESSION
            result.reasons.append(
                f"Market closed — last M5 candle {int(age.total_seconds() // 60)} min ago"
            )
            return result

        if self.instrument.check_funding:
            result.funding_rate = await self.fetcher.fetch_funding_rate()

        return self.evaluate(
            h4=data["h4"],
            h1=data["h1"],
            m5=data["m5"],
            result=result,
        )

    def evaluate(
        self,
        h4: List[Candle],
        h1: List[Candle],
        m5: List[Candle],
        result: AnalysisResult,
    ) -> AnalysisResult:
        """Evaluate rules 1-8 on the given candles (pure, testable)."""
        result.profile_key = self.profile.key

        # Rule 1 — H4 global trend
        result.h4_trend = detect_trend(h4)
        direction = None
        result.direction_source = "h4"
        if result.h4_trend == Trend.UP:
            direction = Direction.LONG
        elif result.h4_trend == Trend.DOWN:
            direction = Direction.SHORT
        else:
            # H4 has no clean HH+HL / LH+LL. H1 is still a trend, just a
            # lower one — the owner trades those (owner decision 2026-08-06).
            # This is not a counter-trend entry, and it is distinct from the
            # aggressive profile's first-leg H4-CHoCH entry below, which
            # keeps its own label. Applies to both profiles: it is a property
            # of how the owner reads a chart, not a profile decision point.
            h1_trend = detect_trend(h1)
            if h1_trend == Trend.UP:
                direction, result.direction_source = Direction.LONG, "h1"
            elif h1_trend == Trend.DOWN:
                direction, result.direction_source = Direction.SHORT, "h1"
            elif self.profile.allow_h4_choch_entry:
                # aggressive: catch the first leg. Only label the source when
                # a direction actually came out — otherwise direction_source
                # would say "h4_choch" while direction stays None, and the
                # two must never disagree (a result heading for SKIP three
                # lines below has no business claiming a direction source).
                choch_direction = h4_choch_direction(h4)
                if choch_direction is not None:
                    direction, result.direction_source = choch_direction, "h4_choch"
        if direction is None:
            result.verdict = Verdict.SKIP
            result.reasons.append("H4 is flat or CHoCH against the trend — no direction")
            result.watch_notes.append(
                "Wait for a clear HH+HL or LH+LL structure on H4 "
                "(2 closed bodies beyond the extreme)"
            )
            return result

        # Rule 2 — H1 zone
        zone = find_h1_zone(h1, direction, max_touches=self.profile.max_zone_touches)
        if zone is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"H4 is {'bullish' if direction == Direction.LONG else 'bearish'}, "
                "but H1 has no valid untested "
                f"{'Demand' if direction == Direction.LONG else 'Supply'} zone"
            )
            result.watch_notes.append(
                "Wait for a fresh H1 zone to form (an untested "
                f"{'HL' if direction == Direction.LONG else 'LH'})"
            )
            return result
        result.h1_zone = zone

        # Rule 3 phase 1/2 — has price pulled back into the zone?
        span = zone_touch_span(m5, zone)
        if span is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"Price has not reached the H1 {'Demand' if zone.is_demand else 'Supply'} "
                f"zone ({zone.bottom:.2f}–{zone.top:.2f}) yet — pullback phase"
            )
            result.watch_notes.append(
                f"Set an alert at {zone.top if zone.is_demand else zone.bottom:.2f} — "
                "on zone touch, check M5 for a CHoCH + FVG"
            )
            result.watch_notes.append(
                f"Invalidation: H1 body close "
                f"{'below ' + format(zone.bottom, '.2f') if zone.is_demand else 'above ' + format(zone.top, '.2f')}"
            )
            return result
        touch = span[0]

        # Zone still valid on M5? (body close through the far edge = invalidated)
        for c in m5[touch:]:
            if zone.is_demand and c.close < zone.bottom and c.body_low < zone.bottom:
                result.verdict = Verdict.SKIP
                result.reasons.append(
                    f"Price closed below the H1 Demand zone ({zone.bottom:.2f}) — invalidated"
                )
                return result
            if not zone.is_demand and c.close > zone.top and c.body_high > zone.top:
                result.verdict = Verdict.SKIP
                result.reasons.append(
                    f"Price closed above the H1 Supply zone ({zone.top:.2f}) — invalidated"
                )
                return result

        # Price is in a live (non-invalidated) zone — arm the zone-touch ping
        result.in_zone = True

        # Rule 3 phase 2 — M5 CHoCH in trend direction inside the zone
        choch = find_choch(m5, direction, touch)
        if choch is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                "Price is in the H1 zone, but M5 has not printed a CHoCH in the trend direction yet"
            )
            result.watch_notes.append(
                f"Wait for a {'bullish' if direction == Direction.LONG else 'bearish'} "
                f"M5 CHoCH + FVG ≥ {self._fvg_size_label()} inside the zone"
            )
            return result

        # Rule 4 — valid FVG on/after the CHoCH. The imbalance belongs to the
        # impulse leg that breaks structure, so the 3-candle window may end on
        # the CHoCH candle itself (hence choch - 2), but never before the touch.
        same_day = self.profile.fvg_day_scope or self.instrument.source == "crypto"
        fvg = select_valid_fvg(
            m5,
            direction,
            max(touch, choch - 2),
            self._effective_min_fvg,
            same_day_scope=same_day,
        )
        if fvg is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                "M5 CHoCH is there, but no valid FVG — "
                + self._fvg_rejection_detail(m5, direction, max(touch, choch - 2), same_day)
            )
            result.watch_notes.append("Wait for an impulse FVG to form on M5")
            return result

        # Rule 5 — entry level: proximal FVG boundary
        entry = fvg.top if direction == Direction.LONG else fvg.bottom

        # Rule 6 — stop behind the swept extreme of the zone excursion
        extreme = sweep_extreme(m5, direction, touch, choch)
        if direction == Direction.LONG:
            stop_loss = extreme - self.sl_buffer
        else:
            stop_loss = extreme + self.sl_buffer

        risk = abs(entry - stop_loss)
        if risk <= 0:
            result.verdict = Verdict.SKIP
            result.reasons.append("Invalid trade geometry: SL at the entry level")
            return result

        # Rule 5.1 (owner decision 2026-08-05, demoted to a label 2026-08-06)
        # — entry staleness. Replaying without this check, 80-90% of limit
        # orders never filled; but the owner sets his own entry, so this warns
        # instead of suppressing. A negative gap means price has not run past
        # the entry, so market entries never trigger it.
        price = result.price or m5[-1].close
        gap = price - entry if direction == Direction.LONG else entry - price
        if gap > self.max_entry_gap_r * risk:
            result.warnings.append(
                f"price has run {gap / risk:.1f}R past the imbalance"
            )

        # Rule 7 (owner decision 2026-08-05, demoted to a label 2026-08-06) —
        # the nearest unswept liquidity is the pool the move is reaching for,
        # one buffer short of the level so the trade is out before the sweep
        # itself. Detector mode: the arithmetic still runs and still labels a
        # thin objective, but the owner computes RR himself and picks his own
        # target off the ladder, so nothing here suppresses the announcement.
        # Liquidity uses the raw per-instrument minimum FVG as its tolerance —
        # it is a property of the chart, not of the profile the owner is
        # trading, so this reads Instrument.min_fvg directly rather than
        # self.min_fvg_size (which a caller could override independently of
        # the instrument).
        tolerance = self.instrument.min_fvg
        levels = (
            find_liquidity(m5, "M5", tolerance)
            + find_liquidity(h1, "H1", tolerance)
            + find_liquidity(h4, "H4", tolerance)
        )
        target = nearest_liquidity(levels, direction, entry)
        ladder = liquidity_ladder(levels, direction, entry)
        take_profit = None
        rr = 0.0
        if target is None:
            result.warnings.append("no unswept liquidity ahead")
        else:
            if direction == Direction.LONG:
                take_profit = target.price - self.sl_buffer
                reward = take_profit - entry
            else:
                take_profit = target.price + self.sl_buffer
                reward = entry - take_profit
            if reward <= 0:
                # The pool sits inside the buffer: the objective would land on
                # the wrong side of the entry. Report it, do not invent a TP.
                take_profit, target = None, None
                result.warnings.append(
                    "nearest liquidity sits inside the stop buffer"
                )
            else:
                rr = reward / risk
                if rr < self.min_rr:
                    result.warnings.append(
                        f"RR to the nearest liquidity is 1:{rr:.1f}"
                    )

        # The deeper M5 limit option (owner request 2026-08-06) and the
        # untested zones ahead — shown, never preferred: which entry to take
        # is the owner's call.
        # The walk is floored at the zone touch — the excursion this setup
        # formed in — and the candidate is kept only when it really is the
        # deeper entry the alert calls it (see find_order_block).
        order_block = find_order_block(
            m5, direction, fvg.index - 1, touch, entry, stop_loss
        )
        # The ladder is the OTHER untested zones on the trade's own side, so
        # the live entry zone is excluded rather than shown as its own rung.
        zones_ahead = zone_ladder(h1, direction, entry, exclude=zone)

        # Rule 8 — position size hint
        lot_hint = self._lot_hint(entry, risk)

        # Market entry allowed only if price is inside the FVG right now
        # (`price` was already bound by the Rule 5.1 block above)
        entry_is_market = fvg.bottom <= price <= fvg.top

        d = self.instrument.price_decimals
        result.setup = TradeSetup(
            direction=direction,
            entry=round(entry, d),
            stop_loss=round(stop_loss, d),
            take_profit=round(take_profit, d) if take_profit is not None else None,
            rr=round(rr, 2),
            fvg=fvg,
            entry_is_market=entry_is_market,
            lot_hint=lot_hint,
            target=target,
            order_block=order_block,
            ladder=ladder,
            zones_ahead=zones_ahead,
        )
        result.verdict = (
            Verdict.APPROVED_MARKET if entry_is_market else Verdict.APPROVED_LIMIT
        )

        # Rule 9.3 — funding rate advisory (crypto only)
        if result.funding_rate is not None:
            rate = result.funding_rate
            if direction == Direction.LONG and rate > FUNDING_DANGER:
                result.funding_warning = (
                    f"Funding {rate * 100:.3f}%/8h > 0.1% — longs are at elevated "
                    "squeeze risk. Consider SKIP or a smaller size."
                )
            elif abs(rate) > FUNDING_WARN:
                result.funding_warning = (
                    f"Funding {rate * 100:.3f}%/8h is in the 0.05–0.1% zone — your call."
                )

        return result

    def _fvg_size_label(self) -> str:
        """Human threshold: '$2.00' for crypto, '5 pips' for forex."""
        return self._fmt_size(self._effective_min_fvg, precise=False)

    def _fmt_size(self, value: float, precise: bool = True) -> str:
        """Format a price distance: dollars for crypto, pips for forex."""
        if self.instrument.source == "crypto":
            return f"${value:.2f}"
        pips = value / self.instrument.pip
        return f"{pips:.1f} pips" if precise else f"{pips:.0f} pips"

    def _fvg_rejection_detail(
        self, m5, direction: Direction, from_index: int, same_day: bool
    ) -> str:
        """Explain why Rule 4 rejected the impulse (for logs and /check)."""
        rejected = best_rejected_fvg(
            m5, direction, from_index, self._effective_min_fvg, same_day_scope=same_day
        )
        if rejected is None:
            return "no FVG has formed in the impulse yet"
        candidate, problems = rejected
        parts = []
        if "size" in problems:
            parts.append(
                f"size {self._fmt_size(candidate.size)} < required "
                f"{self._fvg_size_label()}"
            )
        if "closed" in problems:
            parts.append("invalidated (body closed through the gap)")
        elif "fill" in problems:
            parts.append(f"{candidate.fill_pct * 100:.0f}% filled (max 50%)")
        if "session" in problems:
            parts.append("formed in a previous session")
        return "best candidate: " + ", ".join(parts)

    def _lot_hint(self, entry: float, risk: float) -> Optional[str]:
        """Rule 8: position size from deposit and SL distance."""
        if not self.deposit:
            return None
        risk_usd = self.deposit * self.risk_pct / 100.0
        if self.instrument.source == "crypto":
            qty = risk_usd / risk
            base = self.display_symbol[:3]
            return (
                f"{qty:.4f} {base} (risk ${risk_usd:.2f} = {self.risk_pct:.1f}% "
                f"of ${self.deposit:.0f} deposit)"
            )
        # Forex: pip value per standard lot (100k); non-USD quote converts by price
        sl_pips = risk / self.instrument.pip
        quote = self.display_symbol[3:]
        pip_value = (
            self.instrument.pip * 100_000
            if quote == "USD"
            else self.instrument.pip * 100_000 / entry
        )
        lots = risk_usd / (sl_pips * pip_value)
        return (
            f"≈{lots:.2f} lots (SL {sl_pips:.0f} pips, risk ${risk_usd:.2f} "
            f"= {self.risk_pct:.1f}% of ${self.deposit:.0f} deposit)"
        )
