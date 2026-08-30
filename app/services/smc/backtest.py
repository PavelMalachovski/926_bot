"""Walk-forward backtest of the Triple Sync strategy on historical candles.

Phase 1 of the money-machine plan (spec 2026-08-30, D20): the reference
backtest drives the PRODUCTION code — `TripleSyncEngine.evaluate` for
detection, `smc_watcher._setup_fingerprint` for dedup, `SignalJournal`
(`record` + `evaluate_signal`) for outcomes — so a simulated signal is the
signal the live bot would have recorded, byte for byte. No network in this
module; candles arrive from `history.py` via the CLI.

Deliberate v1 simplifications, each stated in every report header:
- The news blackout is OFF (D21): a historical red-news calendar is not
  available, so the bias is toward MORE signals than live, never different
  ones.
- Discipline (Rule 10 / Rule 0.2) is not simulated — it counts the owner's
  `taken` marks, which history does not have. Reports assume every signal
  taken.
- The funding-rate advisory (Rule 9.3) is not replayed — historical funding
  is not fetched; it labels a message and never gates a setup.
- Correlation limits between pairs are not applied (v1 runs pairs
  independently).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import structlog

from app.services.smc.engine import TripleSyncEngine
from app.services.smc.instruments import get_instrument
from app.services.smc.journal import SignalJournal, evaluate_signal
from app.services.smc.models import AnalysisResult, Candle, Verdict
from app.services.smc.sessions import active_session, session_block

logger = structlog.get_logger(__name__)

# The exact windows the live fetchers serve (`fetch_all_timeframes`):
# the engine must never see more history than production would.
H4_WINDOW = 300
H1_WINDOW = 400
M5_WINDOW = 400

_M5 = timedelta(minutes=5)
_H1 = timedelta(hours=1)
_H4 = timedelta(hours=4)


class _NeverFetch:
    """Engine fetcher slot for a backtest engine: evaluate() never fetches,
    and if anything ever calls analyze() by mistake it must fail loudly
    rather than quietly hit the network."""

    def __getattr__(self, name):
        raise RuntimeError("backtest engines must not fetch — use evaluate()")


def build_backtest_engine(pair_key: str, profile=None) -> TripleSyncEngine:
    """The watcher's own engine construction, minus the fetcher.

    Mirrors `smc_watcher._build_engine` (same settings source) so every
    threshold — min_rr, max_entry_gap_r, tp1_r, runner_r, pd_basis — is the
    one production runs with.
    """
    from app.core.config import settings
    from app.services.smc.profiles import CONSERVATIVE

    smc = settings.smc
    return TripleSyncEngine(
        instrument=get_instrument(pair_key),
        min_rr=smc.min_rr,
        max_entry_gap_r=smc.max_entry_gap_r,
        tp1_r=smc.tp1_r,
        runner_r=smc.runner_r,
        pd_basis=smc.pd_basis,
        risk_pct=smc.risk_pct,
        deposit=smc.deposit,
        enforce_sessions=smc.enforce_sessions,
        profile=profile or CONSERVATIVE,
        fetcher=_NeverFetch(),
    )


@dataclass
class SignalRecord:
    """One recorded signal plus the context the journal row does not keep."""

    signal: Dict
    warnings: List[str]
    direction_source: str
    session_block: Optional[str]
    fingerprint: str

    def result_r(self) -> Optional[float]:
        """Realized R for a resolved, filled signal; None when unresolved
        or never filled. Hybrid rows carry `result_r` from the journal;
        plain rows (RANGE, D14) map tp -> +rr, sl -> -1 — the same candle-
        touch semantics `evaluate_signal` resolved them with."""
        s = self.signal
        if s.get("result_r") is not None:
            return s["result_r"]
        status = s["status"]
        if status == "tp":
            return s.get("rr") or 0.0
        if status == "sl":
            return -1.0
        return None


@dataclass
class BacktestRun:
    pair: str
    profile_key: str
    start: datetime
    end: datetime
    records: List[SignalRecord] = field(default_factory=list)
    verdicts: Dict[str, int] = field(default_factory=dict)
    cycles: int = 0
    off_session: int = 0
    warmup_skipped: int = 0
    deduped: int = 0


def _window_end(candles: List[Candle], now: datetime, span: timedelta) -> int:
    """Index one past the last candle CLOSED at `now` (closed candles only)."""
    i = len(candles)
    while i > 0 and candles[i - 1].timestamp + span > now:
        i -= 1
    return i


def run_backtest(
    pair: str,
    h4: List[Candle],
    h1: List[Candle],
    m5: List[Candle],
    start: datetime,
    end: datetime,
    journal: SignalJournal,
    engine: Optional[TripleSyncEngine] = None,
    profile=None,
    require_full_windows: bool = True,
) -> BacktestRun:
    """Replay [start, end] the way the watcher lives it.

    One cycle per closed M5 candle: outcome tracking first (the journal
    advances on every closed candle, in and out of session, exactly like
    `evaluate_signal`'s watermark allows), then — in session only — the
    engine checklist on the same windows the live fetchers would have
    served, then the watcher's dedup before recording.

    `require_full_windows` skips cycles until every timeframe can fill its
    production window — live never runs on less; tests with tiny synthetic
    fixtures turn it off.
    """
    engine = engine or build_backtest_engine(pair, profile)
    instrument = get_instrument(pair)
    run = BacktestRun(
        pair=pair,
        profile_key=engine.profile.key,
        start=start,
        end=end,
    )
    last_fingerprint: Optional[str] = None
    from smc_watcher import _setup_fingerprint

    h4_end = h1_end = 0
    for i, candle in enumerate(m5):
        now = candle.timestamp + _M5  # the moment this candle closed
        if now < start or now > end:
            continue

        # Outcomes advance on every closed candle, session or not — a
        # pending order fills on the touch whenever it happens, and expiry
        # (`expires_at`, OPEN_TIMEOUT) is judged against the same `now`.
        for signal in journal.signals:
            if signal["pair"] == pair and signal["status"] in (
                "pending", "open", "open_runner",
            ):
                evaluate_signal(signal, [candle], now)

        session = active_session(
            now, require_weekday=instrument.source == "forex"
        )
        if session is None:
            run.off_session += 1
            continue

        # Same closed-candle windows the live fetchers serve.
        while h4_end < len(h4) and h4[h4_end].timestamp + _H4 <= now:
            h4_end += 1
        while h1_end < len(h1) and h1[h1_end].timestamp + _H1 <= now:
            h1_end += 1
        m5_view = m5[max(0, i + 1 - M5_WINDOW):i + 1]
        h4_view = h4[max(0, h4_end - H4_WINDOW):h4_end]
        h1_view = h1[max(0, h1_end - H1_WINDOW):h1_end]
        if require_full_windows and (
            len(h4_view) < H4_WINDOW
            or len(h1_view) < H1_WINDOW
            or len(m5_view) < M5_WINDOW
        ):
            run.warmup_skipped += 1
            continue

        result = AnalysisResult(
            symbol=pair,
            verdict=Verdict.SKIP,
            checked_at=now,
            price_decimals=instrument.price_decimals,
        )
        result.session_name = session
        result.price = m5_view[-1].close
        result = engine.evaluate(h4=h4_view, h1=h1_view, m5=m5_view, result=result)
        run.cycles += 1
        run.verdicts[result.verdict.value] = (
            run.verdicts.get(result.verdict.value, 0) + 1
        )

        if result.verdict in (Verdict.APPROVED_LIMIT, Verdict.APPROVED_MARKET):
            fingerprint = _setup_fingerprint(result)
            if fingerprint == last_fingerprint:
                run.deduped += 1
                continue
            signal = journal.record(result)
            last_fingerprint = fingerprint
            run.records.append(
                SignalRecord(
                    signal=signal,
                    warnings=list(result.warnings),
                    direction_source=result.direction_source,
                    session_block=session_block(now),
                    fingerprint=fingerprint,
                )
            )

    return run


# --------------------------------------------------------------------------
# Metrics


RESOLVED = ("tp", "sl", "tp1_be", "tp1_runner")
WINS = ("tp", "tp1_runner")


@dataclass
class Stats:
    """Outcome statistics over one group of signal records."""

    n: int = 0
    filled: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0  # tp1_be — TP1 banked, runner stopped at break-even
    expired: int = 0
    timeout: int = 0
    unresolved: int = 0
    total_r: float = 0.0
    profit_r: float = 0.0
    loss_r: float = 0.0
    max_consecutive_losses: int = 0
    max_drawdown_r: float = 0.0

    @property
    def resolved(self) -> int:
        return self.wins + self.losses + self.scratches

    @property
    def winrate(self) -> Optional[float]:
        decided = self.wins + self.losses
        return self.wins / decided if decided else None

    @property
    def expectancy_r(self) -> Optional[float]:
        return self.total_r / self.resolved if self.resolved else None

    @property
    def profit_factor(self) -> Optional[float]:
        if self.loss_r == 0:
            return None
        return self.profit_r / self.loss_r


def compute_stats(records: List[SignalRecord]) -> Stats:
    stats = Stats(n=len(records))
    streak = 0
    equity = peak = 0.0
    ordered = sorted(
        records,
        key=lambda r: r.signal.get("resolved_at") or r.signal["created_at"],
    )
    for record in ordered:
        signal = record.signal
        status = signal["status"]
        if signal.get("filled_at"):
            stats.filled += 1
        if status == "expired":
            stats.expired += 1
            continue
        if status == "timeout":
            stats.timeout += 1
            continue
        if status not in RESOLVED:
            stats.unresolved += 1
            continue
        if status in WINS:
            stats.wins += 1
        elif status == "sl":
            stats.losses += 1
        else:
            stats.scratches += 1
        r = record.result_r() or 0.0
        stats.total_r += r
        if r > 0:
            stats.profit_r += r
        else:
            stats.loss_r += -r
        streak = streak + 1 if status == "sl" else 0
        stats.max_consecutive_losses = max(stats.max_consecutive_losses, streak)
        equity += r
        peak = max(peak, equity)
        stats.max_drawdown_r = max(stats.max_drawdown_r, peak - equity)
    return stats


# The ⚠️ labels the engine attaches (Rules 5.1 / 7, demoted to warnings by
# detector mode) — the autopsy asks which of them predicted losers.
WARNING_BUCKETS: List[Tuple[str, str]] = [
    ("stale entry (Rule 5.1)", "price has run"),
    ("thin RR (Rule 7)", "RR to the"),
    ("no liquidity ahead", "no unswept liquidity"),
    ("target inside stop buffer", "inside the stop buffer"),
]


def warning_autopsy(
    records: List[SignalRecord],
) -> List[Tuple[str, Stats, Stats]]:
    """(label, with-warning stats, without-warning stats) per bucket."""
    out = []
    for label, needle in WARNING_BUCKETS:
        with_w = [
            r for r in records if any(needle in w for w in r.warnings)
        ]
        without = [
            r for r in records if not any(needle in w for w in r.warnings)
        ]
        if with_w:
            out.append((label, compute_stats(with_w), compute_stats(without)))
    return out


def _fmt(value: Optional[float], pattern: str = "{:.2f}") -> str:
    return pattern.format(value) if value is not None else "—"


def _stats_line(label: str, stats: Stats) -> str:
    winrate = (
        f"{stats.winrate * 100:.0f}%" if stats.winrate is not None else "—"
    )
    return (
        f"{label:<28} n={stats.n:<4} filled={stats.filled:<4} "
        f"tp={stats.wins:<3} sl={stats.losses:<3} be={stats.scratches:<3} "
        f"exp={stats.expired:<3} win={winrate:<5} "
        f"exp.R={_fmt(stats.expectancy_r):<6} "
        f"PF={_fmt(stats.profit_factor):<6} "
        f"totR={stats.total_r:+.1f}"
    )


def render_report(run: BacktestRun) -> str:
    """Plain-text report. Header carries the v1 bias disclaimers — a report
    without them overstates what was simulated."""
    lines = [
        f"Backtest {run.pair} [{run.profile_key}] "
        f"{run.start:%Y-%m-%d} → {run.end:%Y-%m-%d}",
        "NOT simulated: news blackout (more signals than live, D21), "
        "discipline Rule 10/0.2 (assumes every signal taken), "
        "funding advisory, cross-pair correlation limits.",
        f"cycles={run.cycles} off_session={run.off_session} "
        f"warmup_skipped={run.warmup_skipped} deduped={run.deduped}",
        "verdicts: "
        + ", ".join(f"{k}={v}" for k, v in sorted(run.verdicts.items())),
        "",
    ]
    lines.append(_stats_line("ALL SIGNALS", compute_stats(run.records)))

    def group(title: str, key) -> None:
        buckets: Dict[str, List[SignalRecord]] = {}
        for record in run.records:
            buckets.setdefault(key(record), []).append(record)
        if len(buckets) > 1 or (buckets and title == "by tier"):
            lines.append("")
            lines.append(title)
            for name in sorted(buckets):
                lines.append(
                    _stats_line(f"  {name}", compute_stats(buckets[name]))
                )

    group("by tier", lambda r: r.signal.get("tier") or "regular")
    group("by direction source", lambda r: r.direction_source)
    # `session_block` carries the day ("2026-08-15/New York") for dedup
    # insight; the report aggregates on the block name alone.
    group(
        "by session block",
        lambda r: (r.session_block or "?").split("/", 1)[-1],
    )

    autopsy = warning_autopsy(run.records)
    if autopsy:
        lines.append("")
        lines.append("warning autopsy (with ⚠️ vs without)")
        for label, with_w, without in autopsy:
            lines.append(_stats_line(f"  ⚠️ {label}", with_w))
            lines.append(_stats_line("     without it", without))

    return "\n".join(lines)
