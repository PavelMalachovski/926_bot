"""Session window filter (Rule 0.1) — Prague local time, DST aware.

Trading hours: 08:00-18:30 Prague. Crypto is watched every day, forex only
Monday-Friday. The day is split into two adjacent blocks so the Rule 4
"an FVG does not carry over between sessions" separation is preserved.

The NY block ends at 18:30, not 20:00 (owner decision 2026-08-07): the last
evening hour and a half produced nothing worth trading, and dropping it keeps
four forex pairs inside the Twelve Data free tier (800 credits/day).

WINDOWS is the single source of truth for the trading day — active_session,
session_end_utc (Rule 10 pending-order expiry), same_session (Rule 4 FVG
scope) and the news digest all derive from it. Never hardcode the hours
anywhere else.
"""

from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple

import pytz

PRAGUE = pytz.timezone("Europe/Prague")

# (start, end, name) in Prague local time, year-round (DST follows Prague).
WINDOWS: List[Tuple[time, time, str]] = [
    (time(8, 0), time(14, 0), "Frankfurt/London"),
    (time(14, 0), time(18, 30), "New York"),
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


def session_block(utc_dt: datetime) -> Optional[str]:
    """Stable identity of the session block containing `utc_dt`, or None
    outside trading hours: "2026-08-16/Frankfurt-London".

    The unit of zone-alert silence (owner decision 2026-08-16): one alert
    per zone per block. The Prague date prefix keeps yesterday's London
    block distinct from today's.
    """
    local = to_prague(utc_dt)
    current = local.time()
    for start, end, name in WINDOWS:
        if start <= current < end:
            return f"{local.date().isoformat()}/{name.replace('/', '-')}"
    return None


def mute_deadline(utc_dt: datetime) -> datetime:
    """UTC instant at which a mute pressed at `utc_dt` expires.

    Inside a block that is not the last of the day -> that block's end
    (the owner's "until 14:00"). Inside the last block, or outside trading
    hours altogether -> the next trading day's open (his "until tomorrow
    morning"). Weekends need no special case: nothing is watched then, and
    the mute simply expires unused.
    """
    local = to_prague(utc_dt)
    current = local.time()
    for index, (start, end, _) in enumerate(WINDOWS):
        if start <= current < end and index < len(WINDOWS) - 1:
            return PRAGUE.localize(
                datetime.combine(local.date(), end), is_dst=None
            ).astimezone(pytz.UTC)
    open_time = WINDOWS[0][0]
    day = local.date()
    if current >= open_time:
        day = day + timedelta(days=1)
    return PRAGUE.localize(
        datetime.combine(day, open_time), is_dst=None
    ).astimezone(pytz.UTC)


def prague_hhmm(utc_dt: datetime) -> str:
    """Prague wall-clock HH:MM — for button labels and status lines."""
    return to_prague(utc_dt).strftime("%H:%M")
