"""Candles as text for the model.

The model reads numbers, not pixels: the charts it also receives show the
shape, but every level it quotes must come from these rows. Prague time,
oldest first, the last row is the most recent CLOSED candle — the same
contract the fetchers keep for the rule engine.
"""

from typing import List, Sequence

from app.services.smc.models import Candle
from app.services.smc.sessions import to_prague


def candles_text(candles: Sequence[Candle], decimals: int, limit: int) -> str:
    """`dd.mm HH:MM open high low close` rows for the last `limit` candles."""
    rows: List[str] = []
    for c in list(candles)[-limit:]:
        stamp = to_prague(c.timestamp).strftime("%d.%m %H:%M")
        rows.append(
            f"{stamp} {c.open:.{decimals}f} {c.high:.{decimals}f} "
            f"{c.low:.{decimals}f} {c.close:.{decimals}f}"
        )
    return "\n".join(rows)


def candle_block(label: str, candles: Sequence[Candle], decimals: int, limit: int) -> str:
    shown = min(limit, len(candles))
    return f"{label} ({shown} candles):\n" + candles_text(candles, decimals, limit)
