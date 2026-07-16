"""Data structures for the Triple Sync + Imbalance SMC strategy."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


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
    """A fully validated trade proposal."""

    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    fvg: FVG
    entry_is_market: bool = False
    lot_hint: Optional[str] = None


@dataclass
class AnalysisResult:
    """Outcome of one strategy run."""

    symbol: str
    verdict: Verdict
    checked_at: datetime
    price: float = 0.0
    h4_trend: Trend = Trend.FLAT
    h1_zone: Optional[Zone] = None
    setup: Optional[TradeSetup] = None
    funding_rate: Optional[float] = None
    funding_warning: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    watch_notes: List[str] = field(default_factory=list)
    session_name: Optional[str] = None
    price_decimals: int = 2
    # last fetched M5 candles (in-memory only, used for chart rendering)
    m5_candles: Optional[List[Candle]] = field(default=None, repr=False)
