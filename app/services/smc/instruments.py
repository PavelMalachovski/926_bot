"""Instrument registry: per-pair strategy parameters and data source."""

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class Instrument:
    """A tradeable pair with its strategy-specific parameters."""

    key: str  # canonical name used in commands and state, e.g. "ETHUSD"
    source: str  # "crypto" (Binance) | "forex" (OANDA if token set, else Yahoo)
    source_symbol: str  # Binance symbol for crypto, OANDA instrument for forex
    min_fvg: float  # Rule 4: minimal FVG size in price units
    sl_buffer: float  # Rule 6: buffer beyond the M5 pivot in price units
    pip: float  # pip size in price units (for lot math / messages)
    price_decimals: int  # formatting precision
    check_funding: bool  # Rule 9.3: funding rate advisory (crypto only)


# Strategy universe (Rule "Торгуемые инструменты").
# FVG minimums per Rule 4: forex pairs 5 pips, ETHUSD $2.
INSTRUMENTS: Dict[str, Instrument] = {
    "ETHUSD": Instrument(
        key="ETHUSD",
        source="crypto",
        source_symbol="ETHUSDT",
        min_fvg=2.0,
        sl_buffer=2.0,
        pip=0.01,
        price_decimals=2,
        check_funding=True,
    ),
    "USDJPY": Instrument(
        key="USDJPY",
        source="forex",
        source_symbol="USD_JPY",
        min_fvg=0.050,  # 5 pips (JPY pip = 0.01)
        sl_buffer=0.015,  # 1.5 pips
        pip=0.01,
        price_decimals=3,
        check_funding=False,
    ),
    "EURUSD": Instrument(
        key="EURUSD",
        source="forex",
        source_symbol="EUR_USD",
        min_fvg=0.00050,  # 5 pips
        sl_buffer=0.00015,  # 1.5 pips
        pip=0.0001,
        price_decimals=5,
        check_funding=False,
    ),
    "GBPUSD": Instrument(
        key="GBPUSD",
        source="forex",
        source_symbol="GBP_USD",
        min_fvg=0.00050,
        sl_buffer=0.00015,
        pip=0.0001,
        price_decimals=5,
        check_funding=False,
    ),
    "USDCAD": Instrument(
        key="USDCAD",
        source="forex",
        source_symbol="USD_CAD",
        min_fvg=0.00050,
        sl_buffer=0.00015,
        pip=0.0001,
        price_decimals=5,
        check_funding=False,
    ),
}

DEFAULT_PAIRS: List[str] = ["ETHUSD", "USDJPY"]


def get_instrument(key: str) -> Instrument:
    return INSTRUMENTS[key.upper()]
