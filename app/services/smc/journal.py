"""Signal journal: records every APPROVED setup and tracks its outcome.

Lifecycle of a signal:

    pending      — limit order waiting for price to reach the entry
    open         — entry touched, position "live"
    tp / sl      — take-profit or stop-loss hit first (same candle -> sl,
                   conservative). Legacy path (no tp1) and any signal
                   without hybrid TP levels always resolves this way.
    expired      — entry never touched before the session ended (Rule 10:
                   a pending order does not survive its session)
    timeout      — open too long without resolution (safety valve)

Phase 2 sniper redesign adds a hybrid partial-close lifecycle, active only
when the signal carries `tp1`/`runner_tp` (actual price levels, set by the
engine — see models.TradeSetup):

    open         — entry touched. TP1 not yet tagged.
    open_runner  — TP1 touched (half closed, stop moved to break-even);
                   internal state, never a terminal status. BE/runner are
                   judged starting the candle AFTER the TP1 candle.
    tp1_be       — stopped at break-even after TP1 (or an OPEN_TIMEOUT while
                   running the runner leg — TP1 is already banked, so this
                   times out to BE rather than a bare "timeout").
    tp1_runner   — runner leg hit its target.

`result_r` carries the realized R once a signal resolves (hybrid path
only): -1.0 (sl), +tp1_r*0.5 (tp1_be), +tp1_r*0.5 + runner_r*0.5
(tp1_runner), 0.0 (expired/timeout). Ported from the validated `sn_exit.py`
replay harness (see C:\\temp\\926_bot_data\\scripts\\sn_exit.py), with one
deliberate difference: timeout-after-TP1 resolves as tp1_be instead of a
bare timeout (a known Phase 1 harness simplification, fixed here).

Signals live in the SQLite database (see db.py); outcomes are evaluated from
closed M5 candles, incrementally per cycle.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import structlog

from app.services.smc.db import Database
from app.services.smc.models import AnalysisResult, Candle, Direction
from app.services.smc.notifier import escape_html
from app.services.smc.sessions import session_end_utc, to_prague

logger = structlog.get_logger(__name__)

OPEN_TIMEOUT = timedelta(days=5)


_BAD_TIMESTAMP_SEEN: set = set()


def _parse(ts: str) -> datetime:
    """Parse a persisted ISO timestamp, tolerating poison values.

    A legacy JSON import (see db.py's migrate_legacy_json) can carry a
    naive timestamp (no tzinfo — raises TypeError on the first
    aware-vs-naive comparison downstream) or, rarer, outright unparseable
    garbage (raises ValueError). Before this, either one raised out of
    `evaluate_signal` -> `update_pair` and killed journal tracking on every
    future cycle, forever, since resolving the poisoned row is exactly the
    code path that crashed.

    - naive -> treated as UTC (attach tzinfo).
    - unparseable -> treated as "very old" (datetime.min, UTC) so the
      signal resolves as expired/timed-out instead of looping.

    Logged once per offender (module-level seen-set) so a poisoned row does
    not spam the log every time it is re-evaluated. Mirrors the defensive
    style `Watcher._warn_data_source_failure` uses for the same class of
    bug.
    """
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        if ts not in _BAD_TIMESTAMP_SEEN:
            _BAD_TIMESTAMP_SEEN.add(ts)
            logger.error(
                "Unparseable journal timestamp — treating as very old",
                value=ts,
            )
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def evaluate_signal(signal: Dict, candles: List[Candle], now: datetime) -> Dict:
    """Advance one signal's state using closed candles. Returns the signal.

    Only candles that finished after the last evaluation are considered.

    The hybrid partial-close branch (open_runner/tp1_be/tp1_runner) only
    engages when `signal.get("tp1")` is truthy — a legacy row or a signal
    with no hybrid TP levels keeps the plain pending/open/tp/sl/expired/
    timeout lifecycle byte-for-byte.

    A RANGE signal never takes it (D14, owner decision 2026-08-18): its one
    target is the opposite boundary, taken full size. The engine stops
    writing `tp1` for those, and the `zone_kind` test below covers rows
    already stored with the levels the engine used to compute — tracking
    them on `tp1`/`runner_tp` would chase prices outside the very box the
    setup was aiming at, and ignore the `take_profit` that is the trade.
    """
    hybrid = bool(signal.get("tp1")) and signal.get("zone_kind") != "RANGE"
    active_statuses = (
        ("pending", "open", "open_runner") if hybrid else ("pending", "open")
    )
    if signal["status"] not in active_statuses:
        return signal

    is_long = signal["direction"] == Direction.LONG.value
    entry, sl, tp = signal["entry"], signal["stop_loss"], signal["take_profit"]
    watermark = _parse(signal.get("checked_until") or signal["created_at"])

    tp1_r = runner_r = 0.0
    tp1 = runner_tp = None
    if hybrid:
        risk = abs(entry - sl)
        tp1, runner_tp = signal["tp1"], signal["runner_tp"]
        if risk:  # malformed (risk<=0) setups never reach the journal
            tp1_r = abs(tp1 - entry) / risk
            runner_r = abs(runner_tp - entry) / risk

    for candle in candles:
        candle_end = candle.timestamp + timedelta(minutes=5)
        if candle_end <= watermark:
            continue

        if signal["status"] == "pending":
            touched = candle.low <= entry if is_long else candle.high >= entry
            if touched:
                signal["status"] = "open"
                signal["filled_at"] = candle.timestamp.isoformat()
            else:
                continue

        if signal["status"] == "open":
            hit_sl = candle.low <= sl if is_long else candle.high >= sl
            if hybrid:
                hit_tp1 = candle.high >= tp1 if is_long else candle.low <= tp1
                if hit_sl:  # both in one candle -> conservative: count the stop
                    signal["status"] = "sl"
                    signal["result_r"] = -1.0
                elif hit_tp1:
                    signal["status"] = "open_runner"
                    signal["tp1_at"] = candle.timestamp.isoformat()
                    # The candle that tags TP1 necessarily also spans the
                    # entry region it just left — judging BE/runner on it
                    # would be double jeopardy on an intra-candle path we
                    # cannot see. The next candle judges both.
                    continue
            else:
                # Detector mode: a setup with no unswept liquidity ahead has
                # no take-profit. It still fills and still stops out — "TP
                # hit" is simply impossible for it, and no target invented.
                hit_tp = (
                    False
                    if tp is None
                    else (candle.high >= tp if is_long else candle.low <= tp)
                )
                if hit_sl:  # both in one candle -> conservative: count stop
                    signal["status"] = "sl"
                elif hit_tp:
                    signal["status"] = "tp"

        if hybrid and signal["status"] == "open_runner":
            hit_be = candle.low <= entry if is_long else candle.high >= entry
            hit_run = (
                candle.high >= runner_tp if is_long else candle.low <= runner_tp
            )
            if hit_be:  # adverse first, always: runner+BE same candle -> BE
                signal["status"] = "tp1_be"
                signal["result_r"] = 0.5 * tp1_r
            elif hit_run:
                signal["status"] = "tp1_runner"
                signal["result_r"] = 0.5 * tp1_r + 0.5 * runner_r

        if signal["status"] in ("tp", "sl", "tp1_be", "tp1_runner"):
            signal["resolved_at"] = candle.timestamp.isoformat()
            break

    if candles:
        signal["checked_until"] = (
            candles[-1].timestamp + timedelta(minutes=5)
        ).isoformat()

    # Expiry rules
    if signal["status"] == "pending":
        expires = signal.get("expires_at")
        if expires and now > _parse(expires):
            signal["status"] = "expired"
            signal["resolved_at"] = now.isoformat()
            if hybrid:
                signal["result_r"] = 0.0
    elif signal["status"] in ("open", "open_runner"):
        if now - _parse(signal["created_at"]) > OPEN_TIMEOUT:
            if hybrid and signal["status"] == "open_runner":
                # Phase 2 fix (THE deliberate divergence from sn_exit.py):
                # TP1 is already banked when the runner leg times out —
                # resolve it as BE, not a bare timeout that would silently
                # erase the banked R. Known Phase 1 harness simplification.
                signal["status"] = "tp1_be"
                signal["result_r"] = 0.5 * tp1_r
                logger.info(
                    "Signal timed out with TP1 already banked — "
                    "resolving as tp1_be (timeout-from-runner)",
                    id=signal.get("id"),
                )
            else:
                signal["status"] = "timeout"
                if hybrid:
                    signal["result_r"] = 0.0
            signal["resolved_at"] = now.isoformat()
    return signal


def _feature_vector(result: AnalysisResult) -> Dict:
    """What the tier actually measured, alongside what it decided.

    Audit finding F4 (2026-08-26): the journal recorded the verdict
    (`tier`, `tier_missed`) and the outcome (`result_r`) but none of the
    inputs, so no amount of history could answer "is the pd condition
    earning its keep?". Every field here is bookkeeping — `stats_text` cuts
    expectancy by them, and nothing in the strategy reads them back.

    NULL means "not recorded" and never a value: `room_r` is genuinely None
    when no pool sits ahead (unmeasurable, and passing), `sweep` when the
    excursion took nothing, and the whole vector on rows written before
    these columns existed.
    """
    setup = result.setup
    pd_read = result.pd
    return {
        "tier_missed": ",".join(setup.tier_missed),
        "room_r": setup.room_r,
        "sweep": setup.sweep,
        "entry_gap_r": setup.entry_gap_r,
        "pd_pct": pd_read.position if pd_read else None,
        "pd_side": pd_read.label if pd_read else None,
        "pd_basis": pd_read.range.timeframe if pd_read else None,
        "pd_ote": int(pd_read.in_ote) if pd_read else None,
        "direction_source": result.direction_source,
        "h4_trend": result.h4_trend.value if result.h4_trend else None,
        "h1_trend": result.h1_trend.value if result.h1_trend else None,
        "entry_hour": to_prague(result.checked_at).hour,
    }


# The five ⭐ conditions, in `sniper.classify`'s own order. `stats_text` cuts
# expectancy by each: a condition that pays is one whose "clean" side earns
# more than its "missed" side, and one that does not is a filter costing
# setups for nothing.
TIER_CONDITIONS = ("room", "sweep", "pd", "stale", "trend")

# Below this many resolved signals a per-cell average is noise dressed as a
# number. The cell still prints its count, so the owner can see the sample
# growing rather than wonder why a row vanished.
MIN_EDGE_SAMPLE = 3


def _r_cell(rows: List[Dict]) -> str:
    """"7 · +0.42R", or just the count while the sample is too small."""
    if not rows:
        return "—"
    if len(rows) < MIN_EDGE_SAMPLE:
        return f"{len(rows)} · —"
    avg = sum(r["result_r"] for r in rows) / len(rows)
    return f"{len(rows)} · {avg:+.2f}R"


def _edge_lines(recent: List[Dict]) -> List[str]:
    """Expectancy cut by the things that decided the setup (audit F4).

    Only resolved signals with a recorded feature vector count — rows from
    before those columns existed carry NULL everywhere and would otherwise
    land in whichever bucket a `None` happens to fall into. The whole block
    is skipped until at least one such row exists, so /stats does not grow a
    permanently empty section on the day this ships.

    Every cell prints its sample size next to its average, and an average is
    withheld below `MIN_EDGE_SAMPLE`: the point of this block is to end
    guessing, not to replace it with a confident-looking number built on two
    trades.
    """
    scored = [
        s for s in recent
        if s.get("result_r") is not None and s.get("tier_missed") is not None
    ]
    if not scored:
        return []

    out = [
        "",
        f"<b>Edge by condition</b> ({len(scored)} resolved with data)",
        "<pre>",
        f"{'condition':<10}{'missed':>14}{'clean':>14}",
    ]
    for name in TIER_CONDITIONS:
        missed, clean = [], []
        for row in scored:
            flags = [f for f in (row["tier_missed"] or "").split(",") if f]
            (missed if name in flags else clean).append(row)
        out.append(f"{name:<10}{_r_cell(missed):>14}{_r_cell(clean):>14}")
    out.append("</pre>")

    def group(label: str, key: str, order=None) -> None:
        buckets: Dict[str, List[Dict]] = {}
        for row in scored:
            value = row.get(key)
            if value is None:
                continue
            buckets.setdefault(str(value), []).append(row)
        if len(buckets) < 2:
            return  # one bucket is not a comparison
        if not any(len(v) >= MIN_EDGE_SAMPLE for v in buckets.values()):
            return  # every cell would be a count with no average behind it
        keys = order or sorted(buckets)
        rows = [(k, buckets[k]) for k in keys if k in buckets]
        width = max(len(k) for k, _ in rows)
        out.append(f"<b>{escape_html(label)}</b>")
        out.append("<pre>")
        for k, group_rows in rows:
            out.append(f"{escape_html(k):<{width}}   {_r_cell(group_rows)}")
        out.append("</pre>")

    group("Edge by session block", "session")
    group("Edge by zone kind", "zone_kind")
    group("Edge by direction source", "direction_source")

    # Kill-zone evidence: does the hour of entry change what a setup makes?
    # Spread over a trading day, this is the last block to reach a sample
    # worth reading, so it stays hidden until at least one hour does.
    by_hour: Dict[int, List[Dict]] = {}
    for row in scored:
        if row.get("entry_hour") is not None:
            by_hour.setdefault(int(row["entry_hour"]), []).append(row)
    if any(len(v) >= MIN_EDGE_SAMPLE for v in by_hour.values()):
        out.append("<b>Edge by entry hour (Prague)</b>")
        out.append("<pre>")
        for hour in sorted(by_hour):
            out.append(f"{hour:02d}:00   {_r_cell(by_hour[hour])}")
        out.append("</pre>")
    return out


class SignalJournal:
    """SQLite-backed list of signals with summary statistics."""

    def __init__(self, db: Database):
        self.db = db
        self.signals: List[Dict] = db.signals_all()

    def save(self) -> None:
        """Persist every signal in one pass.

        Kept for bulk loads (e.g. a legacy-JSON import populating
        `self.signals` directly) and for tests seeding rows by hand. Everyday
        mutations below use `_persist` instead — re-upserting the whole
        journal on every `record`/`mark_taken`/`attach_message` call is O(N)
        DB writes per 5-minute cycle, growing forever as the journal fills.
        """
        for signal in self.signals:
            self.db.signal_upsert(signal)

    def _persist(self, signal: Dict) -> None:
        """Write one row. `db.signal_upsert` is already guarded against
        `sqlite3.Error` (see `Database._run`), so nothing further to catch
        here."""
        self.db.signal_upsert(signal)

    def record(self, result: AnalysisResult) -> Dict:
        """Store a freshly approved setup."""
        setup = result.setup
        expires = session_end_utc(result.checked_at)
        signal = {
            "id": uuid.uuid4().hex[:10],
            "pair": result.symbol,
            "direction": setup.direction.value,
            "entry": setup.entry,
            "stop_loss": setup.stop_loss,
            "take_profit": setup.take_profit,
            "rr": setup.rr,
            "session": result.session_name,
            "created_at": result.checked_at.isoformat(),
            "expires_at": expires.isoformat() if expires else None,
            # market entries are considered filled immediately
            "status": "open" if setup.entry_is_market else "pending",
            "filled_at": (
                result.checked_at.isoformat() if setup.entry_is_market else None
            ),
            "resolved_at": None,
            "checked_until": None,
            "taken": None,
            "message_id": None,
            "alert_text": None,
            "profile_key": getattr(result, "profile_key", "conservative"),
            "tp1": setup.tp1,
            "runner_tp": setup.runner_tp,
            "tier": "star" if setup.tier_star else "regular",
            "result_r": None,
            "zone_kind": result.h1_zone.kind if result.h1_zone else None,
            **_feature_vector(result),
        }
        self.signals.append(signal)
        self._persist(signal)
        logger.info("Signal recorded", id=signal["id"], pair=signal["pair"])
        return signal

    def discard(self, signal_id: str) -> None:
        """Remove a signal that was recorded but never actually delivered.

        `record()` writes the row before the alert send so the Telegram
        keyboard can carry its id (see `_send_alert`) — if `notifier.send`
        then returns None (Telegram outage, rate limit), that row would
        otherwise survive as an orphan `pending` signal with no message and
        no fingerprint, later resolving as `expired` and polluting /stats.
        """
        self.signals = [s for s in self.signals if s["id"] != signal_id]
        self.db.signal_delete(signal_id)
        logger.info("Signal discarded (send failed)", id=signal_id)

    def get(self, signal_id: str) -> Optional[Dict]:
        for signal in self.signals:
            if signal["id"] == signal_id:
                return signal
        return None

    def attach_message(
        self, signal_id: str, message_id: int, alert_text: str
    ) -> None:
        """Link the Telegram alert message to the signal (live setup card)."""
        signal = self.get(signal_id)
        if signal:
            signal["message_id"] = message_id
            signal["alert_text"] = alert_text
            self._persist(signal)

    def mark_taken(self, signal_id: str, taken: bool) -> Optional[Dict]:
        """Owner pressed ✅ Took it / ❌ Skipped on the alert."""
        signal = self.get(signal_id)
        if signal:
            signal["taken"] = 1 if taken else 0
            self._persist(signal)
            logger.info("Signal marked", id=signal_id, taken=taken)
        return signal

    # ---------------------------------------------------------- discipline

    def discipline_block(
        self, pair: str, direction: str, session: Optional[str], now: datetime
    ) -> Optional[str]:
        """Kill-switch proxies based on trades the owner marked as taken.

        Rule 10: no re-entry on the same pair+direction in the same session
        after a taken stop-loss. Rule 0.2: two taken stops in one day close
        the trading day.
        """
        today = to_prague(now).date()
        taken_sl_today = [
            s
            for s in self.signals
            if s.get("taken") == 1
            and s["status"] == "sl"
            and s.get("resolved_at")
            and to_prague(_parse(s["resolved_at"])).date() == today
        ]
        if len(taken_sl_today) >= 2:
            return "Rule 0.2: two taken stop-losses today — trading day is closed"
        for s in taken_sl_today:
            if (
                s["pair"] == pair
                and s["direction"] == direction
                and s.get("session") == session
            ):
                return (
                    f"Rule 10: {pair} {direction} already stopped out this "
                    "session — no re-entry"
                )
        return None

    def taken_sl_count_today(self, now: datetime) -> int:
        today = to_prague(now).date()
        return sum(
            1
            for s in self.signals
            if s.get("taken") == 1
            and s["status"] == "sl"
            and s.get("resolved_at")
            and to_prague(_parse(s["resolved_at"])).date() == today
        )

    def unresolved_pairs(self) -> List[str]:
        return sorted(
            {
                s["pair"]
                for s in self.signals
                if s["status"] in ("pending", "open", "open_runner")
            }
        )

    def update_pair(self, pair: str, candles: List[Candle]) -> List[Tuple[Dict, str]]:
        """Evaluate all unresolved signals of a pair.

        Returns state-change events as (signal, event) tuples, where event is
        "filled", "tp", "sl", "expired", "timeout", "tp1_be" or "tp1_runner"
        — used to live-update the alert card in Telegram. "open_runner" (TP1
        tagged, runner still live) is an internal state and never an event
        — the card only needs to know when the signal is fully resolved.
        """
        now = datetime.now(tz=timezone.utc)
        events: List[Tuple[Dict, str]] = []
        for signal in self.signals:
            if signal["pair"] != pair or signal["status"] not in (
                "pending", "open", "open_runner",
            ):
                continue
            before = signal["status"]
            evaluate_signal(signal, candles, now)
            after = signal["status"]
            if before == "pending" and after in (
                "open", "open_runner", "tp", "sl", "tp1_be", "tp1_runner",
            ):
                events.append((signal, "filled"))
            if after != before and after in (
                "tp", "sl", "expired", "timeout", "tp1_be", "tp1_runner",
            ):
                events.append((signal, after))
                logger.info(
                    "Signal resolved", id=signal["id"], pair=pair, outcome=after
                )
            # Every signal reaching here was just re-evaluated (its
            # checked_until watermark advances even with no status change) —
            # persist it. This is bounded by this pair's unresolved signals,
            # never the whole journal (other pairs, already-resolved rows).
            self._persist(signal)
        return events

    def stats_text(self, days: int = 30) -> str:
        """Human summary for /stats."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        recent = [s for s in self.signals if _parse(s["created_at"]) >= cutoff]
        if not recent:
            return f"📒 Journal is empty for the last {days} days — no setups yet."

        def count(status, pool=recent):
            return sum(1 for s in pool if s["status"] == status)

        def wins(pool=recent):
            # Phase 2 hybrid lifecycle: a partial close that banked TP1 (BE
            # or runner) is a win, same as a plain "tp" — both close R>0.
            return (
                count("tp", pool) + count("tp1_be", pool) + count("tp1_runner", pool)
            )

        win_total, sl = wins(), count("sl")
        taken = [s for s in recent if s.get("taken") == 1]
        t_win, t_sl = wins(taken), count("sl", taken)
        active = count("pending") + count("open") + count("open_runner")

        lines = [
            f"📒 <b>Signal journal — last {days} days</b>",
            f"Signals: {len(recent)} | marked taken: {len(taken)}",
            f"🎯 TP {win_total} | 🛑 SL {sl} | ⏳ active "
            f"{active} | 🗑 expired {count('expired')}",
            f"Winrate (signals): {_winrate_bar(win_total, sl)}",
        ]
        if t_win + t_sl:
            lines.append(f"Winrate (taken):   {_winrate_bar(t_win, t_sl)}")
        spark = _sparkline(recent)
        if spark:
            lines.append(f"Last outcomes: {spark}")

        resolved_r = [s["result_r"] for s in recent if s.get("result_r") is not None]
        if resolved_r:
            lines.append(
                f"Realized R (hybrid-tracked): {sum(resolved_r):+.1f}R total, "
                f"avg {sum(resolved_r) / len(resolved_r):+.2f}R "
                f"over {len(resolved_r)} resolved"
            )

        by_pair: Dict[str, List[Dict]] = {}
        for s in recent:
            by_pair.setdefault(s["pair"], []).append(s)
        lines.append("")
        lines.append("<b>By pair:</b>")
        for pair in sorted(by_pair):
            group = by_pair[pair]
            lines.append(
                f"• {escape_html(pair)}: {len(group)} setups, "
                f"TP {wins(group)} / SL {count('sl', group)}"
            )

        # Spec watch item: per-pair ⭐ tier performance must stay visible even
        # when it is negative (e.g. USDJPY) — the whole point of the tier
        # split is to catch a star setup underperforming a regular one.
        star_lines = []
        for pair in sorted(by_pair):
            star = [
                s
                for s in by_pair[pair]
                if s.get("tier") == "star" and s.get("result_r") is not None
            ]
            if not star:
                continue
            star_r = sum(s["result_r"] for s in star)
            star_wins = sum(1 for s in star if s["result_r"] > 0)
            star_lines.append(
                f"• {escape_html(pair)}: {len(star)} resolved, {star_wins}W, "
                f"{star_r:+.1f}R"
            )
        if star_lines:
            lines.append("")
            lines.append("<b>⭐ Star tier by pair:</b>")
            lines.extend(star_lines)

        lines.extend(_edge_lines(recent))

        # Detector mode: a setup with no unswept liquidity ahead has no
        # take-profit and carries rr 0.0 — averaging those zeros in would
        # deflate the figure into meaninglessness. They have no planned RR;
        # they are not a planned RR of zero.
        with_target = [s for s in recent if s.get("take_profit") is not None]
        lines.append("")
        if with_target:
            avg_rr = sum(s["rr"] for s in with_target) / len(with_target)
            lines.append(f"Average planned RR: 1:{avg_rr:.1f}")
        missing = len(recent) - len(with_target)
        if missing:
            # Say it even when nothing is left to average, or the RR line the
            # owner is used to would just vanish with no explanation.
            lines.append(
                # Not only "no liquidity ahead": a null take-profit also
                # comes from the nearest pool sitting inside the stop buffer
                # (engine, detector mode). Both mean the same thing here —
                # no structural objective to plan an RR against.
                f"({missing} signal{'' if missing == 1 else 's'} had no "
                "structural objective — no planned RR)"
            )
        # Spec 2026-08-06 §4: the bot records its own reference entry/SL/TP,
        # but the owner sets his own levels. This must not read as his
        # performance — that lives in /journal (MT4 screenshots).
        lines.append(
            "Tracked against the bot's reference levels, not your actual orders."
        )
        return "\n".join(lines)


def _winrate_bar(tp: int, sl: int) -> str:
    """'57% ▰▰▰▰▰▱▱▱' or an em dash when nothing closed yet."""
    closed = tp + sl
    if not closed:
        return "—"
    pct = tp / closed * 100
    filled = round(pct / 12.5)
    return f"{pct:.0f}% {'▰' * filled}{'▱' * (8 - filled)}"


def _sparkline(signals: List[Dict], limit: int = 10) -> str:
    """Emoji strip of the most recent closed outcomes: 🟩 tp/tp1_be/tp1_runner
    (all wins), 🟥 sl, ⬜ expired/timeout."""
    icons = {
        "tp": "🟩",
        "tp1_be": "🟩",
        "tp1_runner": "🟩",
        "sl": "🟥",
        "expired": "⬜",
        "timeout": "⬜",
    }
    closed = [s for s in signals if s["status"] in icons]
    closed.sort(key=lambda s: s.get("resolved_at") or s["created_at"])
    return "".join(icons[s["status"]] for s in closed[-limit:])
