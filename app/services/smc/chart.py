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

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

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
) -> None:
    """Draw a horizontal level line + label.

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
    ax.text(
        x_right, price, f" {label}", color=color, fontsize=9,
        fontweight="bold", va="center", ha="left",
    )


# ~32h of M5 by default: enough context to see the swing HH/HL structure and
# the move into the zone behind the setup. Tunable via SMC_CHART_CANDLES.
CHART_CANDLES = int(os.getenv("SMC_CHART_CANDLES", "384"))


def render_setup_chart(
    result: AnalysisResult, candles_back: int = CHART_CANDLES
) -> Optional[bytes]:
    """Render the approved setup as a PNG (last ~16h of M5). None if no data."""
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

    # FVG box from its formation candle to the right edge
    fvg = setup.fvg
    fvg_start = max(0, len(candles) - (len(result.m5_candles) - fvg.index))
    ax.add_patch(
        Rectangle(
            (fvg_start, fvg.bottom),
            x_right - fvg_start,
            fvg.size,
            facecolor="#26a69a" if fvg.is_bullish else "#ef5350",
            alpha=0.18,
            edgecolor="none",
            zorder=1,
        )
    )

    # Clamp the y-axis to the candle range (extended to bracket entry/SL,
    # which sit close to price by construction) before drawing levels. A
    # liquidity take-profit can be an H4 pool hundreds of points away; left
    # to autoscale, its axhline would flatten the whole chart (Important
    # finding 1). take_profit is deliberately excluded from the window.
    ylim = _price_ylim(candles, (setup.entry, setup.stop_loss))
    ax.set_ylim(*ylim)

    # Entry / SL / TP levels
    d = result.price_decimals
    for price, color, label in (
        (setup.entry, "#2962ff", f"ENTRY {setup.entry:.{d}f}"),
        (setup.stop_loss, "#f23645", f"SL {setup.stop_loss:.{d}f}"),
        (setup.take_profit, "#089981", f"TP {setup.take_profit:.{d}f}"),
    ):
        _level(ax, price, color, label, x_right, y_bounds=ylim)

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
    ax.set_title(
        f"{result.symbol} M5 — {side} setup | RR 1:{setup.rr:.1f} | "
        f"{to_prague(result.checked_at).strftime('%d.%m %H:%M')} Prague",
        color=FG,
        fontsize=11,
        fontweight="bold",
    )

    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()


def render_plan_chart(plan, h1_candles, candles_back: int = 120) -> Optional[bytes]:
    """Render a pre-market plan on H1 candles: zones + projected E/SL/TP.

    `plan` is a plan.PairPlan. Returns None if there is nothing to draw.
    """
    if not h1_candles or not plan.scenarios:
        return None
    candles = list(h1_candles[-candles_back:])
    d = plan.price_decimals

    fig, ax = plt.subplots(figsize=(13, 6), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    _draw_candles(ax, candles)
    x_right = len(candles) + 6

    for s in plan.scenarios:
        zone_color = DEMAND_COLOR if s.direction == Direction.LONG else SUPPLY_COLOR
        ax.axhspan(s.zone_bottom, s.zone_top, color=zone_color, alpha=0.12, zorder=1)

    # Clamp the y-axis to the H1 candle range (extended to bracket every
    # scenario's entry/SL) before drawing levels — same fix as the alert
    # chart (Important finding 1): a liquidity take-profit projected off H4
    # can sit far outside the visible candles.
    in_range: List[Optional[float]] = []
    for s in plan.scenarios:
        in_range.extend([s.entry, s.stop_loss])
    ylim = _price_ylim(candles, in_range)
    ax.set_ylim(*ylim)

    for s in plan.scenarios:
        is_long = s.direction == Direction.LONG
        zone_color = DEMAND_COLOR if is_long else SUPPLY_COLOR
        tag = "L" if is_long else "S"
        _level(ax, s.entry, zone_color, f"{tag} Entry {s.entry:.{d}f}", x_right, ylim)
        _level(ax, s.stop_loss, SUPPLY_COLOR, f"{tag} SL {s.stop_loss:.{d}f}", x_right, ylim)
        _level(ax, s.take_profit, TP_COLOR, f"{tag} TP {s.take_profit:.{d}f}", x_right, ylim)

    _style_axes(ax, candles, x_right, "%d.%m")
    ax.set_title(
        f"{plan.pair} H1 — Pre-Market Plan | price {plan.price:.{d}f}",
        color=FG,
        fontsize=11,
        fontweight="bold",
    )

    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()
