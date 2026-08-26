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
    Zone,
)
from app.services.smc import pd as pd_module
from app.services.smc.profiles import (
    CONSERVATIVE,
    StrategyProfile,
    effective_min_fvg,
)
from app.services.smc.range import (
    Range,
    boundary_excursion_start,
    boundary_swept,
    boundary_zone,
    detect_range,
)
from app.services.smc.sessions import active_session
from app.services.smc import sniper
from app.services.smc.structure import (
    detect_trend,
    find_choch,
    find_h1_fvg_zone,
    find_order_block,
    find_zone_of_interest,
    first_zone_touch,
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


def trends_disagree(h4_trend, h1_trend) -> bool:
    """True only when BOTH timeframes trend and they point opposite ways.

    An H4 FLAT is not a disagreement — it is the H1-fallback case the owner
    approved on 2026-08-06, and it must keep earning the star exactly as it
    does today. An H1 FLAT under a trending H4 is likewise no conflict:
    nothing is arguing.
    """
    trending = (Trend.UP, Trend.DOWN)
    return (
        h4_trend in trending and h1_trend in trending and h4_trend != h1_trend
    )


def _zone_kind_label(zone: Zone) -> str:
    """Name the zone Rule 3's invalidation message is about, in the owner's
    own vocabulary for each kind — a RANGE boundary is not an H1 supply or
    demand zone, so it must not be called one (Task 3 wording fix)."""
    if zone.kind == "RANGE":
        return "range LOW boundary" if zone.is_demand else "range HIGH boundary"
    return f"H1 {'Demand' if zone.is_demand else 'Supply'} zone"


def _zone_broken(candles: List[Candle], zone: Zone) -> bool:
    """Rule 3's invalidation test: has a body closed through the far edge?

    For an OB or an FVG zone this is the historical, one-way answer — the
    first body close beyond the far edge kills the zone permanently, and
    that memory is load-bearing (review 2026-08-11: a stop-hunt through a
    demand zone alerted on re-entry).

    A RANGE boundary is read reclaim-aware instead (D15, owner decision
    2026-08-18): a close beyond the boundary invalidates it only while the
    break STILL HOLDS at the end of the excursion. The pierce D9 blesses —
    price runs the stops above the range high and snaps back inside — is
    usually an M5 body close, so the one-way test killed exactly the
    stop-hunt the owner wants to trade. This mirrors
    `structure._break_still_holds`, which already spares the H4 trend from
    a reclaimed fakeout, and it is deliberately limited to RANGE zones.
    """
    reclaim_aware = zone.kind == "RANGE"
    broken = False
    for c in candles:
        if zone.is_demand:
            if c.close < zone.bottom and c.body_low < zone.bottom:
                broken = True
            elif broken and c.close > zone.bottom:
                broken = False  # boundary reclaimed
        else:
            if c.close > zone.top and c.body_high > zone.top:
                broken = True
            elif broken and c.close < zone.top:
                broken = False
        if broken and not reclaim_aware:
            return True
    return broken


def _is_deeper_than(zone, direction: Direction, entry: float) -> bool:
    """True if `zone` sits further out than `entry` on the trade's own
    side — the same "further out" convention `zone_ladder` uses (`beyond`
    there is this function's `entry`): `zone.bottom > entry` for a SHORT
    (supply further above), `zone.top < entry` for a LONG (demand further
    below)."""
    return zone.bottom > entry if direction == Direction.SHORT else zone.top < entry


class TripleSyncEngine:
    """Runs one full strategy pass for a single instrument."""

    def __init__(
        self,
        instrument: Optional[Instrument] = None,
        min_fvg_size: Optional[float] = None,
        sl_buffer: Optional[float] = None,
        min_rr: float = 1.0,
        max_entry_gap_r: float = 0.75,
        tp1_r: float = 2.0,
        runner_r: float = 3.0,
        pd_basis: str = "h4",
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
        # One shared expression with plan._scenario and the watcher's
        # m5_marks call — see profiles.effective_min_fvg for the only way
        # they can legitimately differ (an explicit min_fvg_size override,
        # which production never passes).
        self._effective_min_fvg = effective_min_fvg(self.min_fvg_size, self.profile)
        self.sl_buffer = (
            sl_buffer if sl_buffer is not None else self.instrument.sl_buffer
        )
        self.min_rr = min_rr
        self.max_entry_gap_r = max_entry_gap_r
        self.tp1_r = tp1_r
        self.runner_r = runner_r
        self.pd_basis = pd_basis
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

        # Rule 1 — H4 global trend. H1 is computed unconditionally right
        # alongside it (Task 4, owner decision D6) so `trends_disagree` can
        # compare the two regardless of which branch below supplies the
        # trade direction — the FLAT branch just reuses this value instead
        # of calling detect_trend(h1) a second time.
        result.h4_trend = detect_trend(h4)
        result.h1_trend = detect_trend(h1)
        direction = None
        # The range boundary the setup forms at, when a range supplied the
        # direction — Rule 2 takes it as the zone of interest, and Rules 6/7
        # read the box off `result.market_range`.
        boundary: Optional[Zone] = None
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
            h1_trend = result.h1_trend
            if h1_trend == Trend.UP:
                direction, result.direction_source = Direction.LONG, "h1"
            elif h1_trend == Trend.DOWN:
                direction, result.direction_source = Direction.SHORT, "h1"
            else:
                # D11 (owner decision 2026-08-18): both timeframes flat — the
                # one state where the bot had nothing to say. A range gives it
                # boundaries to trade between (spec §3.2). A trending H1 is
                # checked first and still wins, so no signal the owner already
                # gets can be replaced by a range one. The tolerance is the
                # RAW per-instrument minimum FVG, like every other clustering
                # and sweep measurement in this package — never the
                # profile-scaled value.
                market_range = detect_range(h1, self.instrument.min_fvg)
                if market_range is not None and not market_range.broken:
                    result.market_range = market_range
                    boundary = self._boundary_in_play(market_range, m5)
                    if boundary is not None:
                        direction = (
                            Direction.LONG if boundary.is_demand
                            else Direction.SHORT
                        )
                        result.direction_source = "range"
                if direction is None and self.profile.allow_h4_choch_entry:
                    # aggressive: catch the first leg. Only label the source
                    # when a direction actually came out — otherwise
                    # direction_source would say "h4_choch" while direction
                    # stays None, and the two must never disagree (a result
                    # heading for SKIP below has no business claiming a
                    # direction source).
                    choch_direction = h4_choch_direction(h4)
                    if choch_direction is not None:
                        direction, result.direction_source = (
                            choch_direction, "h4_choch"
                        )
                if direction is None and result.market_range is not None:
                    # The box is there, price is between its edges: nothing to
                    # trade YET, which is a WATCH rather than a SKIP — exactly
                    # like Rule 2's "no zone" and Rule 3's "not reached" waits.
                    live = result.market_range
                    result.verdict = Verdict.WATCH
                    result.reasons.append(
                        "H4 and H1 are both flat inside a range "
                        f"({live.bottom:.2f}–{live.top:.2f}), but price sits "
                        "mid-range — no boundary to trade from yet"
                    )
                    result.watch_notes.append(
                        f"Set alerts at {live.top:.2f} (short) and "
                        f"{live.bottom:.2f} (long) — on a boundary touch, "
                        "check M5 for a CHoCH + FVG"
                    )
                    return result
        if direction is None:
            result.verdict = Verdict.SKIP
            result.reasons.append("H4 is flat or CHoCH against the trend — no direction")
            result.watch_notes.append(
                "Wait for a clear HH+HL or LH+LL structure on H4 "
                "(2 closed bodies beyond the extreme)"
            )
            return result

        # Rule 2 — H1 zone of interest: order block first, untouched
        # imbalance as the fallback (owner decision D4, spec §2.1). The
        # minimum gap size is the per-instrument floor scaled by the
        # profile, exactly as Rule 4 scales the M5 imbalance.
        # A range setup enters at its boundary: the band Rule 1 already
        # picked IS the zone of interest, and every rule below runs on it
        # unchanged (that is the point of expressing a boundary as a Zone).
        zone = boundary if boundary is not None else find_zone_of_interest(
            h1,
            direction,
            min_size=self._effective_min_fvg,
            max_touches=self.profile.max_zone_touches,
        )
        if zone is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"H4 is {'bullish' if direction == Direction.LONG else 'bearish'}, "
                "but H1 has no valid untested "
                f"{'Demand' if direction == Direction.LONG else 'Supply'} zone "
                "(no order block, no untouched H1 imbalance)"
            )
            # Kept word-for-word identical to plan._zone_note: the plan
            # reports this stage in the live checklist's own words.
            result.watch_notes.append(
                "Wait for a fresh H1 zone to form — an untested "
                f"{'HL' if direction == Direction.LONG else 'LH'}"
                " order block or an untouched H1 imbalance"
            )
            return result
        result.h1_zone = zone

        # Rule 3 phase 1/2 — has price pulled back into the zone?
        span = zone_touch_span(m5, zone)
        if span is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                f"Price has not reached the {_zone_kind_label(zone)} "
                f"({zone.bottom:.2f}–{zone.top:.2f}) yet — pullback phase"
            )
            result.watch_notes.append(
                f"Set an alert at {zone.top if zone.is_demand else zone.bottom:.2f} — "
                "on zone touch, check M5 for a CHoCH + FVG"
            )
            far_edge = zone.bottom if zone.is_demand else zone.top
            beyond = f"{'below' if zone.is_demand else 'above'} {far_edge:.2f}"
            result.watch_notes.append(
                # D15: a RANGE boundary survives a pierce that is reclaimed,
                # so its invalidation is stated the way `_zone_broken` now
                # measures it. OB/FVG wording is unchanged.
                f"Invalidation: a close {beyond} that still holds"
                if zone.kind == "RANGE"
                else f"Invalidation: H1 body close {beyond}"
            )
            return result
        touch = span[0]

        # Zone still valid on M5? A body close through the far edge kills the
        # zone permanently, so the scan anchors at the FIRST touch since the
        # zone formed — `touch` is only the latest excursion, and starting
        # there forgot an invalidation once price exited and re-entered
        # (review 2026-08-11: a stop-hunt through a demand zone alerted on
        # re-entry). The fallback covers candle lists that predate the zone's
        # pivot timestamp, where no post-formation touch exists to anchor on.
        scan_from = first_zone_touch(m5, zone)
        if scan_from is None:
            scan_from = touch
        if _zone_broken(m5[scan_from:], zone):
            far_edge = zone.bottom if zone.is_demand else zone.top
            result.verdict = Verdict.SKIP
            result.reasons.append(
                f"Price closed {'below' if zone.is_demand else 'above'} the "
                f"{_zone_kind_label(zone)} ({far_edge:.2f}) — invalidated"
            )
            return result

        # Price is in a live (non-invalidated) zone — arm the zone-touch ping
        result.in_zone = True

        # Rule 3 phase 2 — M5 CHoCH in trend direction inside the zone
        choch = find_choch(m5, direction, touch)
        if choch is None:
            result.verdict = Verdict.WATCH
            result.reasons.append(
                "Price is at the " + _zone_kind_label(zone) + ", but M5 has "
                "not printed a CHoCH in the trade direction yet"
                if zone.kind == "RANGE"
                else "Price is in the H1 zone, but M5 has not printed a CHoCH "
                     "in the trend direction yet"
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

        # Rule 6 — stop behind the swept extreme of the zone excursion.
        #
        # A RANGE boundary's band is one tolerance thick, so a raid that
        # prints a candle wholly outside it splits the excursion
        # `zone_touch_span` measures and leaves `touch` on the leg price had
        # already returned on — a stop anchored there sits inside the very
        # wick that took the liquidity (spec §3.3, reviewer 2026-08-18).
        # `boundary_excursion_start` heals that split for range setups only;
        # every other zone keeps `touch` exactly as before, and the CHoCH and
        # FVG searches above keep it in every case.
        excursion_start = (
            boundary_excursion_start(m5, zone, touch)
            if boundary is not None
            else touch
        )
        extreme = sweep_extreme(m5, direction, excursion_start, choch)
        if boundary is not None:
            # A range stop belongs beyond the BOUNDARY, not merely beyond a
            # wick that turned back inside it — but it must never sit inside
            # the swept extreme either, so the further of the two wins and
            # the buffer below is applied to it exactly as usual.
            box = result.market_range
            extreme = (
                min(extreme, box.bottom) if direction == Direction.LONG
                else max(extreme, box.top)
            )
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
        stale = gap > self.max_entry_gap_r * risk
        if stale:
            result.warnings.append(
                f"price has run {gap / risk:.1f}R past the imbalance"
            )

        # Phase 2 sniper redesign (owner decision 2026-08-12): hybrid exit
        # levels — TP1 at tp1_r*risk for half the position, a runner at
        # runner_r*risk for the rest — plus the star-tier verdict (room,
        # sweep, premium/discount, staleness; see sniper.py). Detector mode
        # unchanged: the tier only labels the setup, it never suppresses it.
        # `stale` above reuses the exact Rule 5.1 comparison, computed once.
        #
        # D14 (owner decision 2026-08-18): a RANGE setup has NO hybrid exit —
        # one target, the opposite boundary, full size. Risk there is
        # anchored beyond the boundary plus a buffer, so RR across the box is
        # routinely under 2 and a 2R TP1 would land outside the very box the
        # setup aims at. Leaving the levels None is also what makes
        # `journal.evaluate_signal` track the signal on `take_profit` like
        # any other non-hybrid setup, instead of chasing prices the range
        # thesis says are unreachable.
        tp1 = runner_tp = None
        if boundary is None:
            if direction == Direction.LONG:
                tp1 = entry + self.tp1_r * risk
                runner_tp = entry + self.runner_r * risk
            else:
                tp1 = entry - self.tp1_r * risk
                runner_tp = entry - self.runner_r * risk

        # Same raw per-instrument tolerance the Rule 7 block below uses (a
        # property of the chart, not of the profile).
        tier_tolerance = self.instrument.min_fvg
        sweep = sniper.sweep_label(
            m5, h1, direction, touch, choch, tier_tolerance, result.checked_at
        )
        if sweep is None and boundary is not None:
            # D9/D16 (owner decisions 2026-08-18): a boundary pierced and
            # reclaimed is liquidity taken, whether the pierce closed beyond
            # the boundary or only wicked through it — D15 made the
            # body-close raid tradeable, and that raid is the one the owner
            # calls a sweep. `sweep_label` names it only while
            # the boundary's EQH/EQL pool survives `find_liquidity`'s own
            # sweep filter — a pierce deeper than the tolerance deletes the
            # pool, so the label goes blank exactly when the sweep was most
            # convincing.
            #
            # D13 (owner decision 2026-08-18): ask it of THIS setup's
            # excursion — the same touch→CHoCH window `sweep_extreme` is
            # anchored on, and the scope every other sweep check in the bot
            # uses. `Range.swept_*` spans everything since the boundary's
            # last pivot, so reading it here would hand the star to a setup
            # whose own excursion took nothing, and a boundary that was ever
            # raided would star every touch afterwards.
            box = result.market_range
            # The same widened window Rule 6 anchored the stop on: a raid
            # that split the excursion left `touch` after the pierce, so
            # measuring from there would miss the very sweep being asked
            # about.
            excursion = m5[excursion_start:choch + 1]
            if direction == Direction.LONG:
                swept = boundary_swept(excursion, box.bottom, above=False)
            else:
                swept = boundary_swept(excursion, box.top, above=True)
            if swept:
                sweep = "RangeL" if direction == Direction.LONG else "RangeH"
        # Premium/discount (owner decision D17, 2026-08-26). The verdict the
        # tier reads is unchanged — `pd_state` still asks which half of the
        # range the entry sits in — but the range itself is now resolved
        # explicitly: H4 first, because H4 is where Rule 1 takes the
        # direction from, H1 as the fallback, and each accepted only while it
        # still contains the entry. `sniper.dealing_range`'s last-two-H1-
        # pivots box was five to fifteen hourly candles on a timeframe the
        # bias does not come from, and it never left the code as a number.
        # `pd_basis="h1"` restores it verbatim without a deploy.
        result.pd = pd_module.read(h4, h1, entry, direction, basis=self.pd_basis)
        pd = sniper.pd_state(
            direction, entry, result.pd.range.as_tuple() if result.pd else None
        )
        room = sniper.room_r(h1, h4, direction, entry, risk, tier_tolerance)
        tier = sniper.classify(
            room, sweep, pd, stale,
            trend_disagrees=trends_disagree(result.h4_trend, result.h1_trend),
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
        ladder = liquidity_ladder(levels, direction, entry)
        target = None
        take_profit = None
        rr = 0.0
        if boundary is not None:
            # Range mode: Rule 7's nearest pool is not the objective — the
            # opposite boundary is, one buffer short of it so the trade is out
            # before the level. No pool is named, and the "no unswept
            # liquidity ahead" warning must not fire: this setup HAS an
            # objective, it is simply not a liquidity level.
            box = result.market_range
            if direction == Direction.LONG:
                take_profit = box.top - self.sl_buffer
                reward = take_profit - entry
            else:
                take_profit = box.bottom + self.sl_buffer
                reward = entry - take_profit
            if reward <= 0:
                # The opposite edge is closer than the buffer: the objective
                # would land on the wrong side of the entry. Same treatment as
                # a liquidity pool inside the buffer — report it, invent
                # nothing.
                take_profit = None
                result.warnings.append(
                    "the opposite boundary sits inside the stop buffer"
                )
            else:
                rr = reward / risk
                if rr < self.min_rr:
                    result.warnings.append(
                        f"RR to the opposite boundary is 1:{rr:.1f}"
                    )
        elif (target := nearest_liquidity(levels, direction, entry)) is None:
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
        # D4's runner-up: when the order block won Rule 2, the untouched
        # imbalance it beat still belongs in the ladder if it is a genuine
        # deeper entry (spec §2.1) — otherwise it is simply lost.
        if zone.kind == "OB":
            runner_up = find_h1_fvg_zone(h1, direction, self._effective_min_fvg)
            if runner_up is not None and _is_deeper_than(runner_up, direction, entry):
                zones_ahead = [runner_up] + zones_ahead

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
            tp1=round(tp1, d) if tp1 is not None else None,
            runner_tp=round(runner_tp, d) if runner_tp is not None else None,
            tier_star=tier.star,
            tier_missed=tier.missed,
            room_r=room,
            sweep=sweep,
            entry_gap_r=gap / risk,
        )
        result.verdict = (
            Verdict.APPROVED_MARKET if entry_is_market else Verdict.APPROVED_LIMIT
        )

        # Rule 9.3 — funding rate advisory (crypto only). Symmetric by
        # direction: longs pay positive funding, shorts pay negative — a
        # SHORT at -0.15%/8h is squeezed exactly as hard as a LONG at
        # +0.15%/8h. The mild tier states only that the rate is above the
        # FUNDING_WARN advisory floor — no upper-bound (FUNDING_DANGER)
        # claim, since the favorable-direction extreme (e.g. a LONG at
        # -0.15%) also falls through to this branch and would falsify a
        # band claim.
        if result.funding_rate is not None:
            rate = result.funding_rate
            danger_pct = FUNDING_DANGER * 100
            warn_pct = FUNDING_WARN * 100
            if direction == Direction.LONG and rate > FUNDING_DANGER:
                result.funding_warning = (
                    f"Funding {rate * 100:.3f}%/8h > {danger_pct:.2f}% — longs "
                    "are at elevated squeeze risk. Consider SKIP or a smaller "
                    "size."
                )
            elif direction == Direction.SHORT and rate < -FUNDING_DANGER:
                result.funding_warning = (
                    f"Funding {rate * 100:.3f}%/8h < -{danger_pct:.2f}% — shorts "
                    "are at elevated squeeze risk. Consider SKIP or a smaller "
                    "size."
                )
            elif abs(rate) > FUNDING_WARN:
                # Direction-symmetric danger check above only fires for the
                # unfavorable pairing (LONG+positive / SHORT+negative); the
                # favorable-direction extreme (e.g. a LONG at -0.15%) falls
                # through to here too, so this text must hold for ANY
                # abs(rate) > FUNDING_WARN — no upper-bound claim.
                result.funding_warning = (
                    f"Funding {rate * 100:.3f}%/8h is above the "
                    f"{warn_pct:.2f}% advisory level — your call."
                )

        return result

    def _boundary_in_play(
        self, market_range: Range, m5: List[Candle]
    ) -> Optional[Zone]:
        """The boundary price is working, as a Zone, or None mid-range.

        "Price is at this boundary" is the question Rule 3 already asks of
        every zone, so it is asked here in Rule 3's own words —
        `zone_touch_span` — and the excursion it finds is the very one the
        CHoCH, the FVG and the stop are then measured in. When price has
        visited both edges, the edge whose latest excursion STARTED later
        wins — so a candle that reaches an edge while the other edge's run is
        already under way opens the newer run and takes it. Two runs opening
        on the very same candle (a spike across the whole box) go to the top:
        arbitrary, but deterministic.

        The spec phrased this as "the latest closed M5 candle has reached the
        band". Taken literally it can barely ever coexist with a finished
        setup: a valid FVG is at least the minimum imbalance tall — the same
        measure the band is one of — and its newest candle sits that whole
        gap away from the band price fell from, so by the time Rule 4 passes
        the last candle is out of the band by construction, and a range would
        produce WATCHes and nothing else. Asking for the excursion keeps the
        intent (price came to the boundary and turned) while letting the
        setup that follows be announced.
        """
        tolerance = self.instrument.min_fvg
        live: Optional[Zone] = None
        latest = -1
        for direction in (Direction.SHORT, Direction.LONG):
            band = boundary_zone(market_range, direction, tolerance)
            span = zone_touch_span(m5, band)
            if span is not None and span[0] > latest:
                live, latest = band, span[0]
        return live

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
