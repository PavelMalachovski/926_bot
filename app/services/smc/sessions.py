"""Session window filter (Rule 0.1) — Prague local time, DST aware."""

from datetime import datetime, time
from typing import List, Optional, Tuple

import pytz

PRAGUE = pytz.timezone("Europe/Prague")

# (start, end, name) in Prague local time. Summer per strategy spec;
# winter windows are the same blocks shifted one hour back.
SUMMER_WINDOWS = [
    (time(8, 0), time(14, 0), "Frankfurt/London"),
    (time(15, 0), time(22, 0), "New York"),
]
WINTER_WINDOWS = [
    (time(8, 0), time(13, 0), "Frankfurt/London"),
    (time(14, 0), time(21, 0), "New York"),
]


def _is_dst(local_dt: datetime) -> bool:
    return bool(local_dt.dst())


def to_prague(utc_dt: datetime) -> datetime:
    """Convert a UTC datetime (naive or aware) to Prague local time."""
    if utc_dt.tzinfo is None:
        utc_dt = pytz.UTC.localize(utc_dt)
    return utc_dt.astimezone(PRAGUE)


def _windows_for(local_dt: datetime) -> List[Tuple[time, time, str]]:
    return SUMMER_WINDOWS if _is_dst(local_dt) else WINTER_WINDOWS


def active_session(utc_dt: datetime) -> Optional[str]:
    """Return the session name if utc_dt falls inside a trading window, else None."""
    local = to_prague(utc_dt)
    now = local.time()
    for start, end, name in _windows_for(local):
        if start <= now < end:
            return name
    return None


def same_session(utc_a: datetime, utc_b: datetime) -> bool:
    """True if both instants fall inside the same session window on the same day.

    Used for the FVG session rule: a London FVG does not carry into NY.
    """
    a, b = to_prague(utc_a), to_prague(utc_b)
    if a.date() != b.date():
        return False
    for start, end, _ in _windows_for(a):
        a_in = start <= a.time() < end
        b_in = start <= b.time() < end
        if a_in or b_in:
            return a_in and b_in
    return False
