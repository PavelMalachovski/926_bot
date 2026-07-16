"""Session window filter (Rule 0.1) — Prague local time, DST aware.

Trading hours: 08:00-20:00 Prague. Crypto is watched every day, forex only
Monday-Friday. The day is split into two adjacent blocks so the Rule 4
"an FVG does not carry over between sessions" separation is preserved.
"""

from datetime import datetime, time
from typing import List, Optional, Tuple

import pytz

PRAGUE = pytz.timezone("Europe/Prague")

# (start, end, name) in Prague local time, year-round (DST follows Prague).
WINDOWS: List[Tuple[time, time, str]] = [
    (time(8, 0), time(14, 0), "Frankfurt/London"),
    (time(14, 0), time(20, 0), "New York"),
]


def to_prague(utc_dt: datetime) -> datetime:
    """Convert a UTC datetime (naive or aware) to Prague local time."""
    if utc_dt.tzinfo is None:
        utc_dt = pytz.UTC.localize(utc_dt)
    return utc_dt.astimezone(PRAGUE)


def active_session(utc_dt: datetime, require_weekday: bool = False) -> Optional[str]:
    """Return the session name if utc_dt falls inside a trading window.

    With require_weekday=True (forex) Saturday and Sunday return None;
    crypto is watched seven days a week.
    """
    local = to_prague(utc_dt)
    if require_weekday and local.weekday() >= 5:
        return None
    now = local.time()
    for start, end, name in WINDOWS:
        if start <= now < end:
            return name
    return None


def same_trading_day(utc_a: datetime, utc_b: datetime) -> bool:
    """True if both instants fall on the same Prague calendar day.

    Used as the FVG session scope for crypto: 24/7 markets have no
    London/NY liquidity reset, so an FVG stays valid for the whole day.
    """
    return to_prague(utc_a).date() == to_prague(utc_b).date()


def session_end_utc(utc_dt: datetime) -> Optional[datetime]:
    """End (UTC) of the session window containing utc_dt, or None if outside.

    Used for pending-order expiry: a limit order lives only until the end of
    the session it was created in.
    """
    local = to_prague(utc_dt)
    now = local.time()
    for start, end, _ in WINDOWS:
        if start <= now < end:
            end_local = PRAGUE.localize(
                datetime.combine(local.date(), end), is_dst=None
            )
            return end_local.astimezone(pytz.UTC)
    return None


def same_session(utc_a: datetime, utc_b: datetime) -> bool:
    """True if both instants fall inside the same session window on the same day.

    Used for the FVG session rule: a London FVG does not carry into NY.
    """
    a, b = to_prague(utc_a), to_prague(utc_b)
    if a.date() != b.date():
        return False
    for start, end, _ in WINDOWS:
        a_in = start <= a.time() < end
        b_in = start <= b.time() < end
        if a_in or b_in:
            return a_in and b_in
    return False
