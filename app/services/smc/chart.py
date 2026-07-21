"""Setup chart rendering: M5 candles with zone, FVG and entry/SL/TP levels.

Pure matplotlib (Agg backend, no pandas) — the PNG is attached to every
urgent alert so the setup is visible at a glance without opening TradingView.
Rendering failures must never block an alert: callers wrap in try/except.
"""

import os
from io import BytesIO
from typing import Optional

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


def _level(ax, price: float, color: str, label: str, x_right: int) -> None:
    ax.axhline(price, color=color, linewidth=1.1, linestyle="--", zorder=4)
    ax.text(
        x_right, price, f" {label}", color=color, fontsize=9,
        fontweight="bold", va="center", ha="left",
    )


# ~16h of M5 by default: enough to show the swing HH/HL structure behind the
# setup. Tunable via SMC_CHART_CANDLES.
CHART_CANDLES = int(os.getenv("SMC_CHART_CANDLES", "192"))


def render_setup_chart(
    result: AnalysisResult, candles_back: int = CHART_CANDLES
) -> Optional[bytes]:
    """Render the approved setup as a PNG (last ~16h of M5). None if no data."""
    if not result.m5_candles or not result.setup:
        return None
    candles = result.m5_candles[-candles_back:]
    setup = result.setup

    # Wider canvas + thinner wicks so twice as many candles stay legible.
    fig, ax = plt.subplots(figsize=(13, 6), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    # Candles
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

    # Entry / SL / TP levels
    d = result.price_decimals
    for price, color, label in (
        (setup.entry, "#2962ff", f"ENTRY {setup.entry:.{d}f}"),
        (setup.stop_loss, "#f23645", f"SL {setup.stop_loss:.{d}f}"),
        (setup.take_profit, "#089981", f"TP {setup.take_profit:.{d}f}"),
    ):
        ax.axhline(price, color=color, linewidth=1.1, linestyle="--", zorder=4)
        ax.text(
            x_right,
            price,
            f" {label}",
            color=color,
            fontsize=9,
            fontweight="bold",
            va="center",
            ha="left",
        )

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
        is_long = s.direction == Direction.LONG
        zone_color = DEMAND_COLOR if is_long else SUPPLY_COLOR
        ax.axhspan(s.zone_bottom, s.zone_top, color=zone_color, alpha=0.12, zorder=1)
        tag = "L" if is_long else "S"
        _level(ax, s.entry, zone_color, f"{tag} Entry {s.entry:.{d}f}", x_right)
        _level(ax, s.stop_loss, SUPPLY_COLOR, f"{tag} SL {s.stop_loss:.{d}f}", x_right)
        _level(ax, s.take_profit, TP_COLOR, f"{tag} TP {s.take_profit:.{d}f}", x_right)

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
