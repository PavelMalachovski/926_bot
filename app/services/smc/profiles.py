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


def effective_min_fvg(base_min_fvg: float, profile: StrategyProfile) -> float:
    """The minimum imbalance actually in force: the per-instrument floor
    scaled by the profile.

    One expression for every caller — the engine (Rule 2's zone of interest
    and Rule 4's entry gap), `plan._scenario` and the watcher's `m5_marks`
    call each spelled this multiplication out separately, and the plan and
    the live engine naming different H1 zones is exactly the parity the
    OB/FVG zone-of-interest work exists to keep.

    `base_min_fvg` is `Instrument.min_fvg` everywhere in production. The
    engine passes its own `min_fvg_size` instead, which equals the
    instrument's unless a caller overrode it — a tuning hook used by tests
    and `scripts/funnel.py`, never by `_build_engine`. An override is
    therefore the one way the plan and the engine can legitimately disagree
    about this threshold.
    """
    return base_min_fvg * profile.fvg_size_factor
