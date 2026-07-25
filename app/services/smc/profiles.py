"""Strategy profiles: conservative (default) vs aggressive (opt-in).

A profile scales the strategy's strictness without changing any rule. It is
read by the engine at exactly four points (direction, FVG size, zone
selection, FVG scope). Per-instrument thresholds still live in instruments.py;
the profile only multiplies them (fvg_size_factor).

AGGRESSIVE.fvg_size_factor was calibrated to 0.6 via scripts/funnel.py over
45 days of ETHUSD M5 (point-in-time replay, distinct-setup counting): ~4.9
setups/week vs ~1.0 for conservative, while keeping more small-FVG rejection
than 0.4. Re-run the funnel to recalibrate.
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class StrategyProfile:
    key: str
    label: str
    allow_h4_choch_entry: bool  # take direction from an H4 CHoCH when trend is FLAT
    fvg_size_factor: float      # multiplier on Instrument.min_fvg (1.0 = unchanged)
    max_zone_touches: int       # 0 = untested only, 1 = allow one prior retest
    fvg_day_scope: bool         # True = FVG valid all Prague day, False = session block


CONSERVATIVE = StrategyProfile(
    key="conservative",
    label="🛡 Conservative",
    allow_h4_choch_entry=False,
    fvg_size_factor=1.0,
    max_zone_touches=0,
    fvg_day_scope=False,
)

# fvg_size_factor calibrated to 0.6 (see module docstring). Re-run
# scripts/funnel.py to recalibrate.
AGGRESSIVE = StrategyProfile(
    key="aggressive",
    label="⚡ Aggressive",
    allow_h4_choch_entry=True,
    fvg_size_factor=0.6,
    max_zone_touches=1,
    fvg_day_scope=True,
)

PROFILES: Dict[str, StrategyProfile] = {
    CONSERVATIVE.key: CONSERVATIVE,
    AGGRESSIVE.key: AGGRESSIVE,
}


def get_profile(key: Optional[str]) -> StrategyProfile:
    """Look up a profile by key; unknown keys fall back to conservative."""
    return PROFILES.get((key or "").lower(), CONSERVATIVE)
