"""Charts for the Opus bot, on the watcher's matplotlib primitives.

Two kinds: the CONTEXT charts the model receives (H1 and M5, candles and
the current price, nothing else — the numbers come from the rows, the
picture only shows the shape) and the PLAN chart the owner receives (M5
with the entry, the stop, the targets, the invalidation and the watch
zone). Same dark style as the watcher's charts. Never let a render block
a message: callers wrap in try/except.

Uses pyplot's global figure like `smc.chart`, so every call site runs
under the bot's one lock.
"""

from io import BytesIO
from typing import Optional, Sequence

import matplotlib.pyplot as plt

from app.services.opus.plan import OpusPlan
from app.services.smc.chart import (
    BG, DEMAND_COLOR, FG, FVG_COLOR, SUPPLY_COLOR, TP_COLOR, _draw_candles,
    _level, _price_ylim, _style_axes,
)
from app.services.smc.i18n import t
from app.services.smc.instruments import Instrument
from app.services.smc.models import Candle

PRICE_COLOR = "#e0e0e0"
INVALIDATION_COLOR = "#ff7043"
WATCH_COLOR = FVG_COLOR


def render_context_chart(
    candles: Sequence[Candle], pair: str, tf: str, instrument: Instrument,
    candles_back: int,
) -> Optional[bytes]:
    """Plain candles + the last close, for the model."""
    shown = list(candles)[-candles_back:]
    if not shown:
        return None
    d = instrument.price_decimals
    fig, ax = plt.subplots(figsize=(14, 6), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    _draw_candles(ax, shown)
    x_right = len(shown) + 6
    ylim = _price_ylim(shown, [])
    ax.set_ylim(*ylim)
    price = shown[-1].close
    _level(ax, price, PRICE_COLOR, f"{price:.{d}f}", x_right, ylim, [])
    _style_axes(ax, shown, x_right, "%d.%m %H:%M" if tf != "H4" else "%d.%m")
    ax.set_title(f"{pair} {tf}", color=FG, fontsize=11, fontweight="bold")
    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()


def render_plan_chart(
    plan: OpusPlan, m5: Sequence[Candle], instrument: Instrument,
    candles_back: int = 200,
) -> Optional[bytes]:
    """M5 candles with the plan's levels, for the owner."""
    shown = list(m5)[-candles_back:]
    if not shown:
        return None
    d = instrument.price_decimals
    fig, ax = plt.subplots(figsize=(14, 6), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    _draw_candles(ax, shown)
    x_right = len(shown) + 6
    in_range = [plan.entry, plan.stop_loss, plan.invalidation, plan.watch_low, plan.watch_high]
    ylim = _price_ylim(shown, in_range)
    ax.set_ylim(*ylim)
    placed: list = []
    side_color = DEMAND_COLOR if plan.is_long else SUPPLY_COLOR
    if plan.has_watch_zone:
        ax.axhspan(plan.watch_low, plan.watch_high, color=WATCH_COLOR, alpha=0.15, zorder=1)
        ax.text(
            -0.6, (plan.watch_low + plan.watch_high) / 2, t("WATCH"),
            color=WATCH_COLOR, fontsize=8, fontweight="bold", va="center", ha="left",
        )
    if plan.entry is not None:
        label = t("Entry") if plan.action != "market" else t("Market")
        _level(ax, plan.entry, side_color, f"{label} {plan.entry:.{d}f}", x_right, ylim, placed)
    if plan.stop_loss is not None:
        _level(ax, plan.stop_loss, SUPPLY_COLOR, f"SL {plan.stop_loss:.{d}f}", x_right, ylim, placed)
    if plan.tp1 is not None:
        _level(ax, plan.tp1, TP_COLOR, f"TP1 {plan.tp1:.{d}f}", x_right, ylim, placed)
    if plan.tp2 is not None:
        _level(ax, plan.tp2, TP_COLOR, f"TP2 {plan.tp2:.{d}f}", x_right, ylim, placed)
    if plan.invalidation is not None:
        _level(
            ax, plan.invalidation, INVALIDATION_COLOR,
            f"{t('Cancel')} {plan.invalidation:.{d}f}", x_right, ylim, placed,
        )
    _style_axes(ax, shown, x_right, "%H:%M")
    ax.set_title(
        t("{pair} M5 — Opus plan {action} | price {price}",
          pair=plan.pair, action=plan.action.upper(), price=f"{shown[-1].close:.{d}f}"),
        color=FG, fontsize=11, fontweight="bold",
    )
    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buffer.getvalue()
