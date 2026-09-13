"""Setup chart rendering: M5 candles with zone, FVG and entry/SL/TP levels.

Pure matplotlib (Agg backend, no pandas) — the PNG is attached to every
urgent alert so the setup is visible at a glance without opening TradingView.
Rendering failures must never block an alert: callers wrap in try/except.
"""

import os
from io import BytesIO
from typing import List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")

# NOTE: this module uses pyplot's global figure registry (plt.subplots /
# plt.close), which is only safe because every render call site in
# smc_watcher.py runs under the watcher's cycle lock (_get_cycle_lock) —
# one render happens at a time, so there is never a second thread mutating
# pyplot's global state concurrently. If pair processing is ever
# parallelized (concurrent cycles/render calls), this module must migrate
# to the object-oriented Figure/FigureCanvasAgg API first (each render gets
# its own Figure instance instead of sharing pyplot's global one).
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from app.services.smc.i18n import t  # noqa: E402
from app.services.smc.models import AnalysisResult, Direction  # noqa: E402
from app.services.smc.sessions import to_prague  # noqa: E402

UP_COLOR = "#26a69a"
DOWN_COLOR = "#ef5350"
BG = "#131722"
FG = "#d1d4dc"
GRID = "#2a2e39"
DEMAND_COLOR = "#2962ff"
SUPPLY_COLOR = "#f23645"
TP_COLOR = "#089981"
# The 5m order-block box on the setup chart and the H1 zone bands on the
# plan chart (spec 2026-08-16 §2.4) share this amber for the same reason:
# on either chart, amber means order block. This is a deliberate split from
# the level lines, which stay coloured by direction (DEMAND_COLOR/
# SUPPLY_COLOR) everywhere — do not "fix" plan-chart zone bands to match
# the level-line colours, that would erase the OB/FVG distinction §2.4
# exists to draw.
OB_COLOR = "#ffb300"
FVG_COLOR = "#ab47bc"
# Range boundaries (spec 2026-08-16 §3.4, owner: "отметить этот боковик на
# графике чёрным пунктиром" — mark the range with a black dashed line).
# Literal black (#000000) is invisible against BG ("#131722", itself nearly
# black); this near-black grey still reads as "black" against it.
RANGE_COLOR = "#4d4d4d"


def _draw_candles(ax, candles) -> None:
    """Draw OHLC candles on ax at integer x positions."""
    for i, c in enumerate(candles):
        color = UP_COLOR if c.close >= c.open else DOWN_COLOR
        ax.plot([i, i], [c.low, c.high], color=color, linewidth=0.7, zorder=2)
        body_bottom = min(c.open, c.close)
        body_height = max(abs(c.close - c.open), (c.high - c.low) * 0.02 or 1e-9)
        ax.add_patch(
            Rectangle(
                (i - 0.35, body_bottom),
                0.7,
                body_height,
                facecolor=color,
                edgecolor=color,
                zorder=3,
            )
        )


def _style_axes(ax, candles, x_right: int, tf_label: str) -> None:
    ticks = list(range(0, len(candles), max(1, len(candles) // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [to_prague(candles[i].timestamp).strftime(tf_label) for i in ticks],
        color=FG,
        fontsize=8,
    )
    ax.tick_params(colors=FG, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.4, alpha=0.6)
    ax.set_xlim(-1, x_right + 8)


# Fraction of the (candle + entry/SL) span added above and below as breathing
# room when clamping the y-axis. Keeps the price action from touching the
# frame edge without letting a single outlier level reopen the autoscale.
YLIM_MARGIN_FRAC = 0.08


def _price_ylim(
    candles: Sequence, in_range_prices: Sequence[Optional[float]],
    margin_frac: float = YLIM_MARGIN_FRAC,
) -> Tuple[float, float]:
    """Y-axis window: candle high/low extended to bracket entry/SL, plus a
    margin. Deliberately excludes the take-profit — a liquidity target can
    sit hundreds of points away, and letting it into this computation is
    exactly the autoscale blowout this function exists to prevent.
    """
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    hi = max(highs)
    lo = min(lows)
    for p in in_range_prices:
        if p is not None:
            hi = max(hi, p)
            lo = min(lo, p)
    span = hi - lo
    if span <= 0:
        span = max(abs(hi), 1.0) * 0.02
    margin = span * margin_frac
    return lo - margin, hi + margin


def _level(
    ax,
    price: float,
    color: str,
    label: str,
    x_right: int,
    y_bounds: Optional[Tuple[float, float]] = None,
    placed: Optional[list] = None,
) -> None:
    """Draw a horizontal level line + label.

    `placed` (a list the caller shares between calls) stacks labels that
    would overprint each other: an entry and a stop $9 apart on a $160
    chart land on the same pixels (2026-09-10 owner screenshot), so the
    text slides to the nearest free slot while the line stays on price.

    When `y_bounds` is given and `price` falls outside it, an autoscaling
    `axhline` would reopen the y-axis to include it (the bug this guards
    against — a far-away liquidity target flattening the whole chart).
    Instead draw a fixed edge annotation naming the price and the direction
    it sits in, so the owner still sees the number.
    """
    if y_bounds is not None:
        lo, hi = y_bounds
        if price > hi:
            ax.annotate(
                f"{label} ↑",
                xy=(0.99, 0.98), xycoords="axes fraction",
                color=color, fontsize=9, fontweight="bold",
                ha="right", va="top",
                bbox=dict(boxstyle="round,pad=0.25", fc=BG, ec=color, lw=0.8),
                zorder=5,
            )
            return
        if price < lo:
            ax.annotate(
                f"{label} ↓",
                xy=(0.99, 0.02), xycoords="axes fraction",
                color=color, fontsize=9, fontweight="bold",
                ha="right", va="bottom",
                bbox=dict(boxstyle="round,pad=0.25", fc=BG, ec=color, lw=0.8),
                zorder=5,
            )
            return
    ax.axhline(price, color=color, linewidth=1.1, linestyle="--", zorder=4)
    label_y = price
    if placed is not None and y_bounds is not None:
        label_y = _stacked_y(price, placed, y_bounds)
    ax.text(
        x_right, label_y, f" {label}", color=color, fontsize=9,
        fontweight="bold", va="center", ha="left", zorder=6,
        # a ground under the text: the dashed line otherwise runs straight
        # through the digits and reads as strike-through on a phone
        bbox=dict(boxstyle="round,pad=0.15", fc=BG, ec="none"),
    )


# One label is about this fraction of the axis height at fontsize 8-9 on
# the 13x6in / 110dpi figure; two labels closer than that overprint.
LABEL_SEP_FRAC = 0.05


def _stacked_y(y: float, placed: list, y_bounds: Tuple[float, float]) -> float:
    """The y (data units) to draw a label at so it clears every label in
    `placed`, nudged away from the neighbour on the side it came from.
    Appends the chosen y to `placed`. Pure, so it is unit-tested."""
    lo, hi = y_bounds
    sep = (hi - lo) * LABEL_SEP_FRAC
    chosen = y
    for _ in range(20):
        clash = [p for p in placed if abs(p - chosen) < sep]
        if not clash:
            break
        nearest = min(clash, key=lambda p: abs(p - chosen))
        chosen = nearest + sep if y >= nearest else nearest - sep
    placed.append(chosen)
    return chosen


# The AI setup box (owner decision D28, 2026-09-13): TradingView's position
# tool — a red box from the entry to the stop, a green one from the entry
# to the target, the entry line itself in white so it reads over both.
AI_RISK_COLOR = "#f23645"
AI_REWARD_COLOR = "#089981"
AI_ENTRY_COLOR = "#ffffff"


def _draw_proposal(
    ax, proposal, x_start: float, x_right: float, ylim: Tuple[float, float],
    decimals: int, placed: list,
) -> None:
    """Draw Claude's proposed order as a position box (D28). Nothing is
    drawn for a "none" proposal. The reward box is clipped to the visible
    window — the target can be an H4 pool far outside it — and the target
    price is still named in the label so the number is never lost."""
    if proposal is None or not getattr(proposal, "is_trade", False):
        return
    lo, hi = ylim
    entry, stop, target = proposal.entry, proposal.stop, proposal.target
    width = max(x_right - x_start, 1.0)

    def box(y0: float, y1: float, color: str) -> None:
        bottom, top = max(min(y0, y1), lo), min(max(y0, y1), hi)
        if top <= bottom:
            return
        ax.add_patch(Rectangle(
            (x_start, bottom), width, top - bottom,
            facecolor=color, alpha=0.16, edgecolor=color, linewidth=0.8,
            linestyle="-", zorder=2,
        ))

    box(entry, stop, AI_RISK_COLOR)
    box(entry, target, AI_REWARD_COLOR)
    d = decimals
    side = "LONG" if proposal.direction == "long" else "SHORT"
    order = "LIMIT" if proposal.order == "limit" else "MARKET"
    # The full order goes into a fixed box in the top-left corner (owner
    # request 2026-09-13: the label on the entry line sat in the zone band
    # among the level labels and was unreadable on a phone). Two lines,
    # solid ground, nothing else is drawn there.
    ax.annotate(
        t("AI {order} {side} {entry}", order=order, side=side, entry=f"{entry:.{d}f}")
        + "\n"
        + t("SL {sl} · TP {tp} · RR 1:{rr}", sl=f"{stop:.{d}f}", tp=f"{target:.{d}f}",
            rr=f"{proposal.rr:.1f}"),
        xy=(0.01, 0.97), xycoords="axes fraction",
        color=AI_ENTRY_COLOR, fontsize=10.5, fontweight="bold",
        ha="left", va="top", zorder=8, linespacing=1.4,
        bbox=dict(boxstyle="round,pad=0.4", fc=BG, ec=AI_ENTRY_COLOR, lw=1.0),
    )
    if lo <= entry <= hi:
        ax.plot([x_start, x_right], [entry, entry], color=AI_ENTRY_COLOR,
                linewidth=1.3, linestyle="-", zorder=5)
        # a short tag on the line itself, so the box and the line pair up
        y = _stacked_y(entry, placed, ylim)
        ax.text(
            x_right, y, " " + t("AI entry"), color=AI_ENTRY_COLOR, fontsize=9,
            fontweight="bold", va="center", ha="left", zorder=7,
            bbox=dict(boxstyle="round,pad=0.15", fc=BG, ec="none"),
        )
    else:
        # the whole order sits outside the window: name it at the edge it
        # sits on, the same way `_level` handles a far-away target
        _level(ax, entry, AI_ENTRY_COLOR, t("AI entry") + f" {entry:.{d}f}",
               int(x_right), ylim, placed)


def _marked_box(
    ax, x0: float, x1: float, bottom: float, top: float, color: str, label: str,
    ylim: Tuple[float, float], placed: list, alpha: float = 0.22,
    hatch: Optional[str] = None,
) -> None:
    """A highlighted band with an outline and a name (owner request
    2026-09-13: the M5 order block and imbalance were faint fills with no
    label — the very bands the AI order rests on). The label sits at the
    box's left edge, stacked so two neighbouring bands keep both names."""
    lo, hi = ylim
    if top <= lo or bottom >= hi:
        return
    ax.add_patch(Rectangle(
        (x0, bottom), max(x1 - x0, 1.0), top - bottom,
        facecolor=color, alpha=alpha, edgecolor=color, linewidth=1.0,
        linestyle="--" if hatch else "-", hatch=hatch, zorder=1,
    ))
    y = _stacked_y((bottom + top) / 2, placed, ylim)
    ax.text(
        x0 + 0.5, y, label, color=color, fontsize=8.5, fontweight="bold",
        va="center", ha="left", zorder=6,
        bbox=dict(boxstyle="round,pad=0.15", fc=BG, ec="none"),
    )


# ~32h of M5 by default: enough context to see the swing HH/HL structure and
# the move into the zone behind the setup. Tunable via SMC_CHART_CANDLES.
CHART_CANDLES = int(os.getenv("SMC_CHART_CANDLES", "384"))


def render_setup_chart(
    result: AnalysisResult, candles_back: int = CHART_CANDLES, proposal=None,
) -> Optional[bytes]:
    """Render the approved setup as a PNG (last ~16h of M5). None if no data.
    `proposal` (D28, an ai_read.AIProposal) adds Claude's order as a box."""
    if not result.m5_candles or not result.setup:
        return None
    candles = result.m5_candles[-candles_back:]
    setup = result.setup

    # Wide canvas + thin wicks so ~384 candles stay legible.
    fig, ax = plt.subplots(figsize=(20, 6), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    # Candles
    for i, c in enumerate(candles):
        color = UP_COLOR if c.close >= c.open else DOWN_COLOR
        ax.plot([i, i], [c.low, c.high], color=color, linewidth=0.6, zorder=2)
        body_bottom = min(c.open, c.close)
        body_height = max(abs(c.close - c.open), (c.high - c.low) * 0.02 or 1e-9)
        ax.add_patch(
            Rectangle(
                (i - 0.35, body_bottom),
                0.7,
                body_height,
                facecolor=color,
                edgecolor=color,
                zorder=3,
            )
        )

    x_right = len(candles) + 6

    # H1 zone (demand/supply)
    if result.h1_zone:
        zone = result.h1_zone
        zone_color = "#2962ff" if zone.is_demand else "#f23645"
        ax.axhspan(zone.bottom, zone.top, color=zone_color, alpha=0.12, zorder=1)

    # Clamp the y-axis to the candle range (extended to bracket entry/SL,
    # which sit close to price by construction) before drawing levels. A
    # liquidity take-profit can be an H4 pool hundreds of points away; left
    # to autoscale, its axhline would flatten the whole chart (Important
    # finding 1). take_profit is deliberately excluded from the window.
    in_range_prices: List[Optional[float]] = [setup.entry, setup.stop_loss]
    if result.market_range is not None:
        in_range_prices.extend(
            [result.market_range.top, result.market_range.bottom]
        )
    if proposal is not None and getattr(proposal, "is_trade", False):
        # the proposed entry and stop sit near price by construction (they
        # are the engine's own levels); the target stays out, like TP
        in_range_prices.extend([proposal.entry, proposal.stop])
    ylim = _price_ylim(candles, in_range_prices)
    ax.set_ylim(*ylim)
    d = result.price_decimals
    band_labels: list = []

    if result.h1_zone:
        zone = result.h1_zone
        if result.market_range is not None and zone.kind == "RANGE":
            zone_name = t("RANGE LOW") if zone.is_demand else t("RANGE HIGH")
        else:
            zone_name = ("Demand" if zone.is_demand else "Supply") + f" {zone.kind}"
        ax.text(
            0.5, _stacked_y((zone.bottom + zone.top) / 2, band_labels, ylim),
            f"H1 {zone_name} {zone.bottom:.{d}f}–{zone.top:.{d}f}",
            color="#2962ff" if zone.is_demand else "#f23645", fontsize=8.5,
            fontweight="bold", va="center", ha="left", zorder=6,
            bbox=dict(boxstyle="round,pad=0.15", fc=BG, ec="none"),
        )

    # FVG box from its formation candle to the right edge. Optional since
    # owner decision D22 (2026-08-30): a setup can form on the CHoCH alone,
    # and the rejected candidate (when there is one) is drawn instead —
    # hatched, so the chart shows the gap the message is talking about
    # without pretending it is the entry. Outlined and named (owner request
    # 2026-09-13): this band is where the limit order rests.
    fvg = setup.fvg or setup.rejected_fvg
    if fvg is not None:
        fvg_start = max(0, len(candles) - (len(result.m5_candles) - fvg.index))
        _marked_box(
            ax, fvg_start, x_right, fvg.bottom, fvg.top, FVG_COLOR,
            f"M5 FVG {fvg.bottom:.{d}f}–{fvg.top:.{d}f}"
            + ("" if setup.fvg is not None else " ✗"),
            ylim, band_labels,
            alpha=0.22 if setup.fvg is not None else 0.08,
            hatch=None if setup.fvg is not None else "///",
        )

    # 5m order block box (the deeper M5 limit option, engine.py) — a second,
    # narrower entry option the owner may take instead of the FVG entry.
    # Optional: not every setup has a qualifying candidate (find_order_block).
    order_block = setup.order_block
    if order_block is not None:
        ob_start = max(
            0, len(candles) - (len(result.m5_candles) - order_block.pivot_index)
        )
        _marked_box(
            ax, ob_start, x_right, order_block.bottom, order_block.top, OB_COLOR,
            f"M5 OB {order_block.bottom:.{d}f}–{order_block.top:.{d}f}",
            ylim, band_labels,
        )

    # Entry / SL / TP levels. The take-profit is optional (detector mode): a
    # setup with no unswept liquidity ahead has no objective to draw, and its
    # line and edge annotation are skipped rather than faked.
    drawn = [
        (setup.entry, "#2962ff", f"{t('ENTRY')} {setup.entry:.{d}f}"),
        (setup.stop_loss, "#f23645", f"SL {setup.stop_loss:.{d}f}"),
    ]
    if setup.take_profit is not None:
        drawn.append(
            (setup.take_profit, "#089981", f"TP {setup.take_profit:.{d}f}")
        )
    placed: list = []
    for price, color, label in drawn:
        _level(ax, price, color, label, x_right, y_bounds=ylim, placed=placed)

    # Range boundaries (spec §3.4). Drawn whenever a range was live,
    # regardless of whether THIS setup's direction came from it —
    # `market_range` being set is enough to draw the box (models.
    # AnalysisResult.market_range docstring): only rendering the whole
    # setup AS a range trade would need the stricter
    # `direction_source == "range"`, and this is not that.
    if result.market_range is not None:
        rng = result.market_range
        _level(ax, rng.top, RANGE_COLOR, t("RANGE HIGH"), x_right, y_bounds=ylim, placed=placed)
        _level(ax, rng.bottom, RANGE_COLOR, t("RANGE LOW"), x_right, y_bounds=ylim, placed=placed)

    # D28: Claude's proposed order, drawn over the last quarter of the
    # window so the box starts where the trade would — now — not at the
    # chart's left edge.
    _draw_proposal(ax, proposal, len(candles) * 0.75, x_right, ylim, d, placed)

    # Sparse Prague time labels on the x axis
    ticks = list(range(0, len(candles), max(1, len(candles) // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [to_prague(candles[i].timestamp).strftime("%H:%M") for i in ticks],
        color=FG,
        fontsize=8,
    )
    ax.tick_params(colors=FG, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.grid(color=GRID, linewidth=0.4, alpha=0.6)
    ax.set_xlim(-1, x_right + 8)

    side = "LONG" if setup.direction == Direction.LONG else "SHORT"
    # No take-profit means no RR to quote — "1:0.0" would read as a measured
    # objective rather than an absent one.
    rr_part = (
        f"RR 1:{setup.rr:.1f}" if setup.take_profit is not None
        else t("no liquidity ahead")
    )
    ax.set_title(
        t("{pair} M5 — {side} setup | {rr} | {when} Prague",
          pair=result.symbol, side=side, rr=rr_part,
          when=to_prague(result.checked_at).strftime('%d.%m %H:%M')),
        color=FG,
        fontsize=11,
        fontweight="bold",
    )

    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()


# Plan-chart zone-band weighting (spec 2026-08-16 §2.4): the winning zone
# is what the plan is actually built on and should read first; the
# runner-up is a deeper alternative shown behind it — dimmer fill, dashed
# edge in the same colour, so the eye lands on the winner first.
WINNER_ZONE_ALPHA = 0.14
RUNNER_UP_ZONE_ALPHA = 0.08


def _zone_kind_color(kind: str) -> str:
    """OB_COLOR/FVG_COLOR/RANGE_COLOR by `Zone.kind` — see the comment at
    those constants for why the plan chart colours zones by kind while its
    level lines stay coloured by direction. A RANGE boundary band is
    neither an order block nor an imbalance, so it gets its own colour
    rather than falling through to FVG_COLOR."""
    if kind == "OB":
        return OB_COLOR
    if kind == "RANGE":
        return RANGE_COLOR
    return FVG_COLOR


def _zone_band(
    ax, bottom: float, top: float, kind: str, alpha: float, dashed: bool = False,
) -> None:
    """Shade one H1 zone band on the plan chart, coloured by kind. The
    runner-up variant (`dashed=True`) gets a dashed edge in the same colour
    at a lower alpha (RUNNER_UP_ZONE_ALPHA) so it reads as secondary."""
    color = _zone_kind_color(kind)
    ax.axhspan(
        bottom, top,
        facecolor=color,
        edgecolor=color if dashed else "none",
        linestyle="--" if dashed else "-",
        linewidth=1.2 if dashed else 0,
        alpha=alpha,
        zorder=1,
    )


def _zone_label(
    ax, bottom: float, top: float, text: str, color: str,
    placed: Optional[list] = None, y_bounds: Optional[Tuple[float, float]] = None,
) -> None:
    """Name a zone band at its left edge, vertically centred in the band —
    placed off the candles (which start at x=0) so it never collides with
    them, and stacked like the level labels so two neighbouring bands (an
    order block and the imbalance right above it) keep both names legible."""
    y = (bottom + top) / 2
    if placed is not None and y_bounds is not None:
        y = _stacked_y(y, placed, y_bounds)
    ax.text(
        -0.6, y, text, color=color, fontsize=8,
        fontweight="bold", va="center", ha="left", zorder=5,
    )


def render_plan_chart(
    plan, h1_candles, candles_back: int = 120, proposal=None, setup=None,
) -> Optional[bytes]:
    """Render a pre-market plan on H1 candles: zones + projected E/SL/TP.

    `plan` is a plan.PairPlan. Returns None if there is nothing to draw —
    unless a `proposal` (D28) is given, which is worth a chart on its own.
    `setup` (a formed TradeSetup) adds its M5 imbalance and order block as
    outlined, named bands over the right third (owner request 2026-09-13):
    on H1 they are a dollar tall and invisible without the outline, and
    they are the bands the AI order rests on.
    """
    has_proposal = proposal is not None and getattr(proposal, "is_trade", False)
    if not h1_candles or (not plan.scenarios and not has_proposal):
        return None
    candles = list(h1_candles[-candles_back:])
    d = plan.price_decimals

    fig, ax = plt.subplots(figsize=(13, 6), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    _draw_candles(ax, candles)
    x_right = len(candles) + 6

    # Clamp the y-axis to the H1 candle range (extended to bracket every
    # scenario's entry/SL, and every drawn zone band including the
    # runner-up) before drawing levels — same fix as the alert chart
    # (Important finding 1): a liquidity take-profit projected off H4 can
    # sit far outside the visible candles, and so can a runner-up zone.
    in_range: List[Optional[float]] = []
    for s in plan.scenarios:
        in_range.extend([s.entry, s.stop_loss])
        if s.runner_up is not None:
            in_range.extend([s.runner_up.bottom, s.runner_up.top])
    if has_proposal:
        in_range.extend([proposal.entry, proposal.stop])
    ylim = _price_ylim(candles, in_range)
    ax.set_ylim(*ylim)
    placed_left: list = []  # zone labels, left edge
    placed_right: list = []  # level labels, right edge
    for s in plan.scenarios:
        side = "Demand" if s.direction == Direction.LONG else "Supply"
        # A RANGE band is a boundary of the box, not a demand/supply zone —
        # label it the way every other range surface does (review
        # 2026-08-18); the runner-up below is OB/FVG only, so it keeps the
        # side wording.
        band_label = (
            (t("RANGE LOW") if s.direction == Direction.LONG else t("RANGE HIGH"))
            if s.kind == "RANGE"
            else f"{side} {s.kind}"
        )
        _zone_band(ax, s.zone_bottom, s.zone_top, s.kind, WINNER_ZONE_ALPHA)
        _zone_label(
            ax, s.zone_bottom, s.zone_top, band_label,
            _zone_kind_color(s.kind), placed_left, ylim,
        )
        if s.runner_up is not None:
            ru = s.runner_up
            _zone_band(
                ax, ru.bottom, ru.top, ru.kind, RUNNER_UP_ZONE_ALPHA, dashed=True,
            )
            _zone_label(
                ax, ru.bottom, ru.top, f"{side} {ru.kind} {t('(alt)')}",
                _zone_kind_color(ru.kind), placed_left, ylim,
            )

    for s in plan.scenarios:
        is_long = s.direction == Direction.LONG
        zone_color = DEMAND_COLOR if is_long else SUPPLY_COLOR
        tag = "L" if is_long else "S"
        _level(
            ax, s.entry, zone_color, f"{tag} {t('Entry')} {s.entry:.{d}f} ({s.kind})",
            x_right, ylim, placed_right,
        )
        _level(
            ax, s.stop_loss, SUPPLY_COLOR, f"{tag} SL {s.stop_loss:.{d}f}",
            x_right, ylim, placed_right,
        )
        _level(
            ax, s.take_profit, TP_COLOR, f"{tag} TP {s.take_profit:.{d}f}",
            x_right, ylim, placed_right,
        )

    # Range boundaries (spec §3.4): one RANGE-kind scenario per side at
    # most (D12) — SHORT projects off the top boundary, LONG off the
    # bottom. In practice the two sides stand or fall together: both are
    # `box height - sl_buffer` of reward over `sl_buffer` of risk, so
    # `plan._range_scenario`'s min_rr filter gives them identical RR and
    # drops both or neither. Each side is still looked up independently
    # here — the geometry is the caller's to change, and a half-drawn box
    # is better than an exception. Entry IS the boundary price for a RANGE
    # scenario (plan.py `_range_scenario`), already folded into `ylim`
    # above via the same entry/stop_loss loop every other scenario uses.
    range_top = next(
        (s.entry for s in plan.scenarios
         if s.kind == "RANGE" and s.direction == Direction.SHORT),
        None,
    )
    range_bottom = next(
        (s.entry for s in plan.scenarios
         if s.kind == "RANGE" and s.direction == Direction.LONG),
        None,
    )
    if range_top is not None:
        _level(ax, range_top, RANGE_COLOR, t("RANGE HIGH"), x_right, ylim, placed_right)
    if range_bottom is not None:
        _level(ax, range_bottom, RANGE_COLOR, t("RANGE LOW"), x_right, ylim, placed_right)

    if setup is not None:
        x0 = len(candles) * 0.7
        fvg = getattr(setup, "fvg", None) or getattr(setup, "rejected_fvg", None)
        if fvg is not None:
            _marked_box(
                ax, x0, x_right, fvg.bottom, fvg.top, FVG_COLOR,
                f"M5 FVG {fvg.bottom:.{d}f}–{fvg.top:.{d}f}"
                + ("" if getattr(setup, "fvg", None) is not None else " ✗"),
                ylim, placed_left, alpha=0.3,
                hatch=None if getattr(setup, "fvg", None) is not None else "///",
            )
        ob = getattr(setup, "order_block", None)
        if ob is not None:
            _marked_box(
                ax, x0, x_right, ob.bottom, ob.top, OB_COLOR,
                f"M5 OB {ob.bottom:.{d}f}–{ob.top:.{d}f}", ylim, placed_left, alpha=0.3,
            )

    # D28: Claude's proposed order as a position box over the right third
    _draw_proposal(ax, proposal, len(candles) * 0.7, x_right, ylim, d, placed_right)

    _style_axes(ax, candles, x_right, "%d.%m")
    ax.set_title(
        t("{pair} H1 — Pre-Market Plan | price {price}",
          pair=plan.pair, price=f"{plan.price:.{d}f}"),
        color=FG,
        fontsize=11,
        fontweight="bold",
    )

    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()
