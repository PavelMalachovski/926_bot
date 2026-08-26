"""Premium/discount: where price sits inside the range it is retracing.

The ⭐ tier has always asked a premium/discount question (`sniper.pd_state`),
but it asked it of whatever box the last two confirmed H1 pivots happened to
make — five to fifteen hourly candles, on a timeframe that is not the one
Rule 1 takes the direction from, and the answer never left the code as a
number. Three setups in a row on 2026-08-26 lost the star to `pd` with no way
to see why.

This module makes the question explicit and answerable:

* the range is named (`DealingRange.timeframe`) and must actually CONTAIN
  price — a box price has already left says nothing about premium or
  discount, so it is refused rather than extrapolated;
* H4 is asked first, because H4 is where the bias comes from, and H1 is the
  fallback (owner decision D17, 2026-08-26 — `SMC_PD_BASIS=h1` restores the
  pre-audit H1-only reading without a code change);
* the position comes out as a fraction of the range, so a message can print
  "24%" instead of a verdict;
* OTE — the 62-79% retracement, the part of the discount the owner actually
  wants to buy in — is a band with prices, not a feeling.

Pure geometry: no network, no DB, no instrument or profile registry. The
caller supplies the candles and the reference price.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from app.services.smc.models import Candle, Direction
from app.services.smc.structure import find_pivots

# The Optimal Trade Entry band, as a share of the leg retraced. 62-79% is the
# owner's own window and the one every SMC desk quotes; it is expressed here
# as retracement depth, and `ote` turns it into two prices for a direction.
OTE_NEAR = 0.62
OTE_FAR = 0.79

DISCOUNT = "discount"
PREMIUM = "premium"
EQUILIBRIUM = "equilibrium"

# How close to the midpoint still reads as equilibrium rather than as a side,
# as a share of the range. Purely a labelling nicety for messages — the ⭐
# gate (`sniper.pd_state`) keeps its own strict `<= mid` / `>= mid` test, so
# nothing about the tier changes because a message says "equilibrium".
EQ_BAND = 0.02


@dataclass(frozen=True)
class DealingRange:
    """The swing low and swing high price is currently trading between."""

    low: float
    high: float
    low_at: datetime
    high_at: datetime
    timeframe: str  # "H4" | "H1"

    @property
    def span(self) -> float:
        return self.high - self.low

    @property
    def equilibrium(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        return self.low <= price <= self.high

    def position(self, price: float) -> float:
        """Where `price` sits in the range: 0.0 at the low, 1.0 at the high.

        Not clamped — a caller that already checked `contains` gets 0..1, and
        one that did not can see how far outside the box price is.
        """
        return (price - self.low) / self.span

    def as_tuple(self) -> Tuple[float, float]:
        """(low, high), the shape `sniper.pd_state` takes."""
        return self.low, self.high


def dealing_range(candles: List[Candle], timeframe: str) -> Optional[DealingRange]:
    """The last confirmed swing low and swing high of `candles`.

    None when either side is missing, or when the low is not below the high
    (a degenerate box: the last confirmed low printed above the last
    confirmed high, which no midpoint can be read off).
    """
    pivots = find_pivots(candles)
    highs = [p for p in pivots if p.is_high]
    lows = [p for p in pivots if not p.is_high]
    if not highs or not lows:
        return None
    low, high = lows[-1], highs[-1]
    if low.price >= high.price:
        return None
    return DealingRange(
        low=low.price,
        high=high.price,
        low_at=low.timestamp,
        high_at=high.timestamp,
        timeframe=timeframe,
    )


def resolve_range(
    h4: List[Candle],
    h1: List[Candle],
    price: float,
    basis: str = "h4",
) -> Optional[DealingRange]:
    """The range premium/discount is measured on, for a reference `price`.

    H4 first (the timeframe Rule 1 takes the direction from), H1 as the
    fallback, and each is accepted only if it actually contains `price`.
    Returning None when neither does is deliberate and matches what the ⭐
    already does with an unreadable range: an unmeasurable condition passes
    (`sniper.classify`). Price outside every box means the market is
    expanding, not retracing, and inventing a verdict for that would be
    a guess dressed as a rule.

    `basis="h1"` asks H1 alone — the pre-audit reading, kept as an escape
    hatch (`SMC_PD_BASIS`) so the tier can be put back without a deploy.
    """
    order = (
        [("H1", h1)] if str(basis).strip().lower() == "h1"
        else [("H4", h4), ("H1", h1)]
    )
    for timeframe, candles in order:
        rng = dealing_range(candles, timeframe)
        if rng is not None and rng.contains(price):
            return rng
    return None


def ote(rng: DealingRange, direction: Direction) -> Tuple[float, float]:
    """The 62-79% retracement band as (low, high) prices.

    A LONG retraces DOWN from the high, so its band sits in the lower third
    of the box; a SHORT retraces UP from the low and sits in the upper third.
    """
    if direction == Direction.LONG:
        return rng.high - OTE_FAR * rng.span, rng.high - OTE_NEAR * rng.span
    return rng.low + OTE_NEAR * rng.span, rng.low + OTE_FAR * rng.span


def side_label(rng: DealingRange, price: float) -> str:
    """"discount" below the midpoint, "premium" above, "equilibrium" on it."""
    pos = rng.position(price)
    if abs(pos - 0.5) <= EQ_BAND:
        return EQUILIBRIUM
    return DISCOUNT if pos < 0.5 else PREMIUM


def favours(direction: Direction, label: str) -> bool:
    """Whether that side of the range is the one this direction wants."""
    if direction == Direction.LONG:
        return label == DISCOUNT
    return label == PREMIUM


@dataclass(frozen=True)
class PDRead:
    """Everything a message needs to say where price is and what that means."""

    range: DealingRange
    price: float
    position: float          # 0..1 inside the box
    label: str               # discount | premium | equilibrium
    ote_low: float
    ote_high: float
    in_ote: bool
    direction: Direction

    @property
    def pct(self) -> int:
        """The position as whole percent, for message text."""
        return int(round(self.position * 100))

    @property
    def favourable(self) -> bool:
        return favours(self.direction, self.label)


def read(
    h4: List[Candle],
    h1: List[Candle],
    price: float,
    direction: Direction,
    basis: str = "h4",
) -> Optional[PDRead]:
    """The full premium/discount picture for `price` in `direction`.

    None when no range contains price — the same "cannot be judged" answer
    `resolve_range` gives, propagated rather than papered over.
    """
    rng = resolve_range(h4, h1, price, basis=basis)
    if rng is None:
        return None
    low, high = ote(rng, direction)
    return PDRead(
        range=rng,
        price=price,
        position=rng.position(price),
        label=side_label(rng, price),
        ote_low=low,
        ote_high=high,
        in_ote=low <= price <= high,
        direction=direction,
    )
