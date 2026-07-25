"""Strategy profiles: conservative (default) vs aggressive (opt-in).

A profile scales the strategy's strictness without changing any rule. It is
read by the engine at exactly four points (direction, FVG size, zone
selection, FVG scope). Per-instrument thresholds still live in instruments.py;
the profile only multiplies them (fvg_size_factor).

AGGRESSIVE.fvg_size_factor is 0.4, chosen via scripts/funnel.py over ~21-45
days of M5 across all pairs (point-in-time replay, distinct-setup counting).
Conservative produces ~0 setups/week on forex and ~1 on ETHUSD; aggressive at
0.4 gives ~5.5/wk on ETHUSD and ~1.4-2.5/wk per forex pair. Forex M5 FVGs are
small relative to the 5-pip minimum, so a higher factor starves forex (0.6 →
~1/wk/pair); 0.4 keeps forex participating while ETHUSD stays sane (not a
firehose — distinct-setup counting confirms ~0.8/day). Re-run the funnel to
recalibrate.
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

# fvg_size_factor calibrated to 0.4 (see module docstring). Re-run
# scripts/funnel.py to recalibrate.
AGGRESSIVE = StrategyProfile(
    key="aggressive",
    label="⚡ Aggressive",
    allow_h4_choch_entry=True,
    fvg_size_factor=0.4,
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
