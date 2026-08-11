"""Data structures for the Triple Sync + Imbalance SMC strategy."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    # liquidity.py imports Candle/Direction/Pivot from this module, so a
    # top-level import here would cycle — the annotation stays a string.
    from app.services.smc.liquidity import LiquidityLevel


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class Trend(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class Verdict(str, Enum):
    APPROVED_LIMIT = "approved_limit"
    APPROVED_MARKET = "approved_market"
    SKIP = "skip"
    WATCH = "watch"  # structure reads clean but the trigger has not formed yet
    OFF_SESSION = "off_session"


@dataclass
class Candle:
    """A single closed OHLC candle. Timestamps are UTC (candle open time)."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass
class Pivot:
    """A confirmed swing point."""

    index: int
    price: float
    timestamp: datetime
    is_high: bool


@dataclass
class Zone:
    """Demand/Supply zone built from a pivot candle."""

    bottom: float
    top: float
    is_demand: bool
    pivot_index: int
    timestamp: datetime
    tested: bool = False
    touches: int = 0
    invalidated: bool = False

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass
class FVG:
    """Fair Value Gap on M5 (3-candle imbalance)."""

    index: int  # index of the newest candle of the triple
    bottom: float
    top: float
    is_bullish: bool
    timestamp: datetime
    fill_pct: float = 0.0
    closed_through: bool = False

    @property
    def size(self) -> float:
        return self.top - self.bottom


@dataclass
class TradeSetup:
    """A detected setup and the levels it implies.

    `take_profit` is optional: a setup with no unswept liquidity ahead (or
    whose nearest pool sits inside the stop buffer) is still a setup, it just
    has no structural objective. `rr` stays 0.0 in that case — nothing
    downstream may divide by it.
    """

    direction: Direction
    entry: float
    stop_loss: float
    take_profit: Optional[float]
    rr: float
    fvg: FVG
    entry_is_market: bool = False
    lot_hint: Optional[str] = None
    target: Optional["LiquidityLevel"] = None
    # Detector mode (2026-08-06): the deeper M5 limit option and the levels
    # ahead, so the owner picks his own entry and objective.
    order_block: Optional["Zone"] = None
    ladder: List["LiquidityLevel"] = field(default_factory=list)
    zones_ahead: List["Zone"] = field(default_factory=list)


@dataclass
class AnalysisResult:
    """Outcome of one strategy run."""

    symbol: str
    verdict: Verdict
    checked_at: datetime
    price: float = 0.0
    h4_trend: Trend = Trend.FLAT
    # Where the trade direction came from: "h4" (a real H4 trend, the normal
    # case), "h1" (H4 was flat, H1 has a clean trend — owner decision
    # 2026-08-06, H1 is a trend too, just a lower one, not a counter-trend
    # entry), or "h4_choch" (aggressive profile only: H4 flat, H1 also flat,
    # direction taken from an unreclaimed H4 CHoCH — first-leg entry, its own
    # separate label). Task 4 renders the alert header from this.
    direction_source: str = "h4"
    h1_zone: Optional[Zone] = None
    setup: Optional[TradeSetup] = None
    funding_rate: Optional[float] = None
    funding_warning: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    watch_notes: List[str] = field(default_factory=list)
    # Detector mode: thresholds annotate instead of suppressing. Each entry is
    # a finished English sentence ready for the alert.
    warnings: List[str] = field(default_factory=list)
    session_name: Optional[str] = None
    price_decimals: int = 2
    profile_key: str = "conservative"
    # True once price is inside a valid, non-invalidated H1 zone (used for the
    # "get ready" zone-touch ping before a full setup forms)
    in_zone: bool = False
    # last fetched M5 candles (in-memory only, used for chart rendering)
    m5_candles: Optional[List[Candle]] = field(default=None, repr=False)
    h4_candles: Optional[List[Candle]] = field(
        default=None, repr=False
    )  # kept for plan recompute
    h1_candles: Optional[List[Candle]] = field(
        default=None, repr=False
    )  # kept for plan recompute/chart
