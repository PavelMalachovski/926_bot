"""Forex Factory red-news filter (Rules -1, 0.3, 0.4).

Uses the official Forex Factory weekly JSON feed (no key, no scraping).
Entries are blocked in a window around every high-impact event:
`before` minutes ahead of the release and `after` minutes past it.

Currency relevance: a forex pair is affected by news for either of its two
currencies; crypto (ETHUSD) is affected only by USD news.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

import httpx
import structlog

from app.services.smc.instruments import Instrument, get_instrument
from app.services.smc.sessions import to_prague

logger = structlog.get_logger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (SMC-Watcher)"}


@dataclass
class NewsEvent:
    time: datetime  # UTC
    currency: str
    title: str

    def prague_hhmm(self) -> str:
        return to_prague(self.time).strftime("%H:%M")


def relevant_currencies(instrument: Instrument) -> Set[str]:
    """Currencies whose red news blocks this instrument."""
    if instrument.source == "crypto":
        return {"USD"}  # ETH is not a news currency; only the dollar leg counts
    return {instrument.key[:3], instrument.key[3:]}


def parse_feed(raw: List[dict]) -> List[NewsEvent]:
    """Extract high-impact events from the FF weekly feed."""
    events = []
    for item in raw:
        if item.get("impact") != "High":
            continue
        try:
            when = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        events.append(
            NewsEvent(
                time=when,
                currency=str(item.get("country", "")).upper(),
                title=str(item.get("title", "")).strip(),
            )
        )
    events.sort(key=lambda e: e.time)
    return events


class NewsCalendar:
    """Cached red-news calendar with blackout checks."""

    def __init__(
        self,
        before_minutes: int = 60,
        after_minutes: int = 15,
        timeout: float = 15.0,
    ):
        self.before = timedelta(minutes=before_minutes)
        self.after = timedelta(minutes=after_minutes)
        self.timeout = timeout
        self.events: List[NewsEvent] = []
        self.fetched_at: Optional[datetime] = None
        self.fetch_error: Optional[str] = None

    async def refresh_if_stale(self, max_age_hours: float = 6.0) -> None:
        """Refetch the feed each morning / every few hours; keep old data on error."""
        now = datetime.now(tz=timezone.utc)
        if self.fetched_at is not None:
            fresh = now - self.fetched_at < timedelta(hours=max_age_hours)
            same_day = to_prague(self.fetched_at).date() == to_prague(now).date()
            if fresh and same_day:
                return
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=HEADERS
            ) as client:
                response = await client.get(FEED_URL)
                response.raise_for_status()
                self.events = parse_feed(response.json())
            self.fetched_at = now
            self.fetch_error = None
            logger.info(
                "Forex Factory calendar refreshed", high_impact=len(self.events)
            )
        except (httpx.HTTPError, ValueError) as e:
            self.fetch_error = str(e)
            logger.warning("Forex Factory feed fetch failed", error=str(e))

    def blackout(
        self, currencies: Set[str], now: Optional[datetime] = None
    ) -> Optional[NewsEvent]:
        """The event whose no-trade window covers `now`, if any."""
        now = now or datetime.now(tz=timezone.utc)
        for event in self.events:
            if event.currency not in currencies:
                continue
            if event.time - self.before <= now <= event.time + self.after:
                return event
        return None

    def upcoming(
        self, currencies: Set[str], within: timedelta, now: Optional[datetime] = None
    ) -> List[NewsEvent]:
        """Events for `currencies` starting within the given horizon."""
        now = now or datetime.now(tz=timezone.utc)
        return [
            e
            for e in self.events
            if e.currency in currencies and now < e.time <= now + within
        ]

    def todays_events(
        self, currencies: Set[str], now: Optional[datetime] = None
    ) -> List[NewsEvent]:
        now = now or datetime.now(tz=timezone.utc)
        today = to_prague(now).date()
        return [
            e
            for e in self.events
            if e.currency in currencies and to_prague(e.time).date() == today
        ]

    def digest_text(
        self, pairs: List[str], now: Optional[datetime] = None
    ) -> str:
        """Morning digest (strategy Rule -1), grouped by session block.

        For every red event: Prague time, title, the watched pairs it hits
        and the exact no-entry window — no abstract rulers to decode.
        """
        from app.services.smc.notifier import escape_html

        now = now or datetime.now(tz=timezone.utc)
        date_str = to_prague(now).strftime("%d.%m.%Y")
        header = f"📅 <b>Forex Factory — {date_str}</b> (Prague time)"
        if self.fetched_at is None:
            return header + "\n⚠️ Calendar not loaded yet" + (
                f" ({self.fetch_error})" if self.fetch_error else ""
            )

        by_pair = {p: relevant_currencies(get_instrument(p)) for p in pairs}
        currencies: Set[str] = set().union(*by_pair.values()) if by_pair else set()
        events = self.todays_events(currencies, now)
        if not events:
            return (
                header
                + f"\n✅ No red news for your pairs "
                f"({', '.join(pairs) if pairs else '—'}) today. Clean hunting."
            )

        def block_of(event: NewsEvent) -> str:
            hour = to_prague(event.time).hour
            if 8 <= hour < 14:
                return "london"
            if 14 <= hour < 20:
                return "ny"
            return "off"

        def event_lines(event: NewsEvent) -> List[str]:
            hits = ", ".join(p for p, cur in by_pair.items() if event.currency in cur)
            start = to_prague(event.time - self.before).strftime("%H:%M")
            end = to_prague(event.time + self.after).strftime("%H:%M")
            return [
                f"🔴 {event.prague_hhmm()} {escape_html(event.title)} "
                f"({event.currency}) → {hits or '—'}",
                f"    ⛔ no entries {start}–{end}",
            ]

        lines = [header, ""]
        for title, key in (
            ("🌅 <b>London 08–14</b>", "london"),
            ("🌇 <b>New York 14–20</b>", "ny"),
        ):
            lines.append(title)
            block_events = [e for e in events if block_of(e) == key]
            if block_events:
                for event in block_events:
                    lines.extend(event_lines(event))
            else:
                lines.append("✅ clear")
        off_hours = [e for e in events if block_of(e) == "off"]
        if off_hours:
            lines.append("🌙 <b>Outside trading hours</b>")
            for event in off_hours:
                lines.extend(event_lines(event))

        lines.append("")
        lines.append(
            f"⛔ Blackout rule: {int(self.before.total_seconds() // 60)} min "
            f"before / {int(self.after.total_seconds() // 60)} min after "
            "each release."
        )
        return "\n".join(lines)
