"""Demo autopilot: PAPER execution of completed setups with a cost model.

Owner decisions D18-D21 (2026-08-28). The bot places its own paper orders
for setups the engine approves and the journal records, then advances them
on the same closed M5 candles the journal reads. No broker, no real money:
this is an internal simulator whose one job is an HONEST forward track —
what the system would have earned net of costs, with nobody's hands on it.

Two tracks, deliberately different:

    journal (signals)   — the MODEL track: touch = fill, no fees, no
                          slippage. Unchanged, comparable with its own
                          history.
    autopilot (orders)  — the PAPER track: strict limit fills (price must
                          trade THROUGH the level), maker/taker fees on
                          every fill, adverse slippage on stop-market
                          exits, GTD expiry at the session end (an order
                          cannot fill on a candle after its expiry, unlike
                          the journal's model), equity-based sizing.

The divergence between the two is a first-class number (audit finding
F1/F2), not a bug.

Lifecycle mirrors journal.evaluate_signal rule for rule (same-candle
conservatism included): pending -> open -> [tp1 -> open_runner ->
(be | runner)] | (sl | tp), plus expiry and the 5-day timeout — with the
one realism change that a timeout CLOSES the position at market instead of
scoring it 0R.

Hard risk gates (D19), enforced in code, not hints:
    - risk per trade: SMC_AUTO_RISK_PCT of paper equity (default 0.5%)
    - day-stop: SMC_AUTO_MAX_SL_DAY realized full stops close the Prague
      trading day (Rule 0.2 as code; the journal's taken-marks discipline
      stays human-only)
    - weekly kill: week net R <= -SMC_AUTO_WEEK_KILL_R blocks new orders
      until next week (/auto on overrides explicitly)
    - max concurrent orders per pair (SMC_AUTO_MAX_OPEN)

Detector mode is untouched: alerts flow exactly as before; the autopilot
only ADDS its own messages. Everything here is synchronous and pure of
network — the watcher feeds it candles and sends its messages.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import structlog

from app.services.smc.db import Database
from app.services.smc.instruments import get_instrument
from app.services.smc.journal import OPEN_TIMEOUT, _parse
from app.services.smc.models import AnalysisResult, Candle
from app.services.smc.notifier import escape_html
from app.services.smc.sessions import prague_hhmm, to_prague

logger = structlog.get_logger(__name__)

ACTIVE_STATUSES = ("pending", "open", "open_runner")

# kv keys (single source of truth for the names)
KV_ENABLED = "auto_enabled"
KV_EQUITY = "auto_equity"
KV_DAY_STOP_NOTIFIED = "auto_day_stop_notified"  # Prague date already alarmed
KV_KILL_NOTIFIED = "auto_kill_notified"  # Prague ISO week already alarmed
KV_KILL_OVERRIDE = "auto_kill_override"  # Prague ISO week /auto on re-armed


@dataclass(frozen=True)
class AutoConfig:
    """Cost model + risk limits, resolved once from settings."""

    pairs: List[str] = field(default_factory=lambda: ["ETHUSD"])
    deposit: float = 10000.0
    risk_pct: float = 0.5
    max_sl_day: int = 2
    week_kill_r: float = 6.0
    max_open: int = 1
    tier: str = "all"  # "all" | "star"
    maker_fee: float = 0.0002  # fraction of notional (0.02%)
    taker_fee: float = 0.0005
    slippage: float = 0.0002  # fraction of price, adverse, stop-market only
    strict_fills: bool = True

    @classmethod
    def from_settings(cls, smc) -> "AutoConfig":
        return cls(
            pairs=smc.auto_pair_list(),
            deposit=smc.auto_deposit,
            risk_pct=smc.auto_risk_pct,
            max_sl_day=smc.auto_max_sl_day,
            week_kill_r=smc.auto_week_kill_r,
            max_open=smc.auto_max_open,
            tier=smc.auto_tier.strip().lower(),
            maker_fee=smc.auto_maker_fee_pct / 100.0,
            taker_fee=smc.auto_taker_fee_pct / 100.0,
            slippage=smc.auto_slippage_pct / 100.0,
            strict_fills=smc.auto_strict_fills,
        )


# --------------------------------------------------------------- pure money


def _gross(is_long: bool, qty: float, fill: float, exit_price: float) -> float:
    """Realized gross PnL of a leg, before fees."""
    move = (exit_price - fill) if is_long else (fill - exit_price)
    return qty * move


def _slip(is_long: bool, price: float, frac: float, *, exiting: bool) -> float:
    """Adverse slippage on a stop-market fill.

    Exiting a LONG (selling into weakness) fills lower; exiting a SHORT
    fills higher. Entering at market is the mirror: a LONG buys higher.
    """
    adverse_down = is_long if exiting else not is_long
    return price * (1 - frac) if adverse_down else price * (1 + frac)


def evaluate_order(
    order: Dict, candles: List[Candle], now: datetime, cfg: AutoConfig
) -> Tuple[Dict, List[Tuple[str, Dict]]]:
    """Advance one paper order on closed M5 candles. Mutates and returns it,
    plus the events that happened: (kind, payload) with kind in "filled",
    "tp1", "closed", "cancelled". Every payload carries "equity_delta" —
    the realized paper-cash change of that event (fees included), which is
    the only way money leaves this function.

    Mirrors journal.evaluate_signal's ordering exactly where they overlap:
    a candle that fills the entry is immediately checked for the stop; SL
    beats TP1/TP inside one candle; the TP1 candle never judges BE/runner
    (the next one does); BE beats the runner inside one candle. The
    differences are the cost model in the module docstring.
    """
    events: List[Tuple[str, Dict]] = []
    if order["status"] not in ACTIVE_STATUSES:
        return order, events

    is_long = order["direction"] == "long"
    qty = order["qty"]
    q_half = qty / 2.0
    entry = order["entry"]
    sl, tp = order["stop_loss"], order["take_profit"]
    tp1, runner_tp = order["tp1"], order["runner_tp"]
    hybrid = bool(tp1) and order.get("zone_kind") != "RANGE"
    watermark = _parse(order.get("checked_until") or order["created_at"])
    expires = _parse(order["expires_at"]) if order.get("expires_at") else None

    def leg_close(exit_level: float, *, taker: bool, half: bool) -> float:
        """Realize one leg: returns the equity delta and books it."""
        exit_price = (
            _slip(is_long, exit_level, cfg.slippage, exiting=True)
            if taker
            else exit_level
        )
        leg_qty = q_half if half else qty
        fee_rate = cfg.taker_fee if taker else cfg.maker_fee
        fee = leg_qty * exit_price * fee_rate
        delta = _gross(is_long, leg_qty, order["fill_price"], exit_price) - fee
        order["fees_usd"] = (order.get("fees_usd") or 0.0) + fee
        order["pnl_usd"] = (order.get("pnl_usd") or 0.0) + delta
        return delta

    def fill_entry(price: float, ts: datetime, *, taker: bool) -> Dict:
        fee_rate = cfg.taker_fee if taker else cfg.maker_fee
        fee = qty * price * fee_rate
        order["status"] = "open"
        order["fill_price"] = price
        order["filled_at"] = ts.isoformat()
        order["fees_usd"] = (order.get("fees_usd") or 0.0) + fee
        order["pnl_usd"] = (order.get("pnl_usd") or 0.0) - fee
        return {"price": price, "equity_delta": -fee}

    def finalize(outcome: str, ts: datetime) -> None:
        order["status"] = "closed"
        order["outcome"] = outcome
        order["resolved_at"] = ts.isoformat()
        risk = order.get("risk_usd") or 0.0
        order["result_r"] = (order["pnl_usd"] / risk) if risk > 0 else None

    for candle in candles:
        candle_end = candle.timestamp + timedelta(minutes=5)
        if candle_end <= watermark:
            continue

        if order["status"] == "pending":
            # GTD: a real broker cancels the order AT expiry — a candle at or
            # after it can no longer fill (stricter than the journal's model,
            # on purpose).
            if expires and candle.timestamp >= expires:
                order["status"] = "cancelled"
                order["outcome"] = "expired"
                order["resolved_at"] = candle.timestamp.isoformat()
                events.append(("cancelled", {"outcome": "expired",
                                             "equity_delta": 0.0}))
                break
            if cfg.strict_fills:
                touched = candle.low < entry if is_long else candle.high > entry
            else:
                touched = candle.low <= entry if is_long else candle.high >= entry
            if touched:
                events.append(
                    ("filled", fill_entry(entry, candle.timestamp, taker=False))
                )
            else:
                continue

        if order["status"] == "open":
            hit_sl = candle.low <= sl if is_long else candle.high >= sl
            if hybrid:
                if cfg.strict_fills:
                    hit_tp1 = candle.high > tp1 if is_long else candle.low < tp1
                else:
                    hit_tp1 = candle.high >= tp1 if is_long else candle.low <= tp1
                if hit_sl:  # both in one candle -> conservative: the stop
                    delta = leg_close(sl, taker=True, half=False)
                    finalize("sl", candle.timestamp)
                    events.append(("closed", {"outcome": "sl",
                                              "equity_delta": delta}))
                elif hit_tp1:
                    delta = leg_close(tp1, taker=False, half=True)
                    order["status"] = "open_runner"
                    order["tp1_at"] = candle.timestamp.isoformat()
                    events.append(("tp1", {"price": tp1,
                                           "equity_delta": delta}))
                    # The TP1 candle spans the entry region it just left —
                    # BE/runner are judged from the next candle (journal
                    # semantics, same reasoning).
                    continue
            else:
                if cfg.strict_fills:
                    hit_tp = (
                        False
                        if tp is None
                        else (candle.high > tp if is_long else candle.low < tp)
                    )
                else:
                    hit_tp = (
                        False
                        if tp is None
                        else (candle.high >= tp if is_long else candle.low <= tp)
                    )
                if hit_sl:
                    delta = leg_close(sl, taker=True, half=False)
                    finalize("sl", candle.timestamp)
                    events.append(("closed", {"outcome": "sl",
                                              "equity_delta": delta}))
                elif hit_tp:
                    delta = leg_close(tp, taker=False, half=False)
                    finalize("tp", candle.timestamp)
                    events.append(("closed", {"outcome": "tp",
                                              "equity_delta": delta}))

        if hybrid and order["status"] == "open_runner":
            be_level = order["fill_price"]
            hit_be = (
                candle.low <= be_level if is_long else candle.high >= be_level
            )
            if cfg.strict_fills:
                hit_run = (
                    candle.high > runner_tp if is_long else candle.low < runner_tp
                )
            else:
                hit_run = (
                    candle.high >= runner_tp
                    if is_long
                    else candle.low <= runner_tp
                )
            if hit_be:  # adverse first, always (journal semantics)
                delta = leg_close(be_level, taker=True, half=True)
                finalize("tp1_be", candle.timestamp)
                events.append(("closed", {"outcome": "tp1_be",
                                          "equity_delta": delta}))
            elif hit_run:
                delta = leg_close(runner_tp, taker=False, half=True)
                finalize("tp1_runner", candle.timestamp)
                events.append(("closed", {"outcome": "tp1_runner",
                                          "equity_delta": delta}))

        if order["status"] in ("closed", "cancelled"):
            break

    if candles:
        order["checked_until"] = (
            candles[-1].timestamp + timedelta(minutes=5)
        ).isoformat()

    # Expiry via wall clock (no candle reached the expiry yet, but the
    # session is over — e.g. a data gap): same rule as the journal.
    if order["status"] == "pending" and expires and now > expires:
        order["status"] = "cancelled"
        order["outcome"] = "expired"
        order["resolved_at"] = now.isoformat()
        events.append(("cancelled", {"outcome": "expired", "equity_delta": 0.0}))

    # Timeout: the journal scores a 5-day-old open signal 0R and forgets it;
    # a real position has to be CLOSED, so the paper track closes at market
    # (last candle close, taker + slippage) — the honest version of the same
    # safety valve. TP1-already-banked keeps its banked half automatically:
    # only the remainder closes here.
    elif order["status"] in ("open", "open_runner") and candles:
        if now - _parse(order["created_at"]) > OPEN_TIMEOUT:
            last_close = candles[-1].close
            half = order["status"] == "open_runner"
            delta = leg_close(last_close, taker=True, half=half)
            finalize("timeout", now)
            events.append(("closed", {"outcome": "timeout",
                                      "equity_delta": delta}))

    return order, events


# ------------------------------------------------------------------ manager


class Autopilot:
    """Owns the paper orders, the equity, and the D19 risk gates."""

    def __init__(self, db: Database, cfg: Optional[AutoConfig] = None):
        self.db = db
        self.cfg = cfg or AutoConfig()
        self.orders: List[Dict] = db.auto_orders_all()
        if db.kv_get(KV_EQUITY) is None:
            db.kv_set(KV_EQUITY, self.cfg.deposit)

    # ------------------------------------------------------------- plumbing

    def _persist(self, order: Dict) -> None:
        self.db.auto_order_upsert(order)

    @property
    def equity(self) -> float:
        value = self.db.kv_get(KV_EQUITY)
        try:
            return float(value)
        except (TypeError, ValueError):
            return self.cfg.deposit

    def _apply_equity(self, delta: float) -> float:
        equity = self.equity + delta
        self.db.kv_set(KV_EQUITY, equity)
        return equity

    @property
    def enabled(self) -> bool:
        value = self.db.kv_get(KV_ENABLED)
        return True if value is None else bool(value)

    def set_enabled(self, enabled: bool, now: Optional[datetime] = None) -> str:
        """/auto on|off. Turning ON while the weekly kill is active is the
        explicit owner override D19 allows — it re-arms the current week."""
        now = now or datetime.now(tz=timezone.utc)
        self.db.kv_set(KV_ENABLED, enabled)
        if not enabled:
            return (
                "🤖 Autopilot <b>OFF</b> — no new paper orders. Open paper "
                "positions keep their exits; /auto on to re-enable."
            )
        note = ""
        if self._week_killed(now):
            self.db.kv_set(KV_KILL_OVERRIDE, self._week_key(now))
            note = (
                "\n⚠️ Weekly kill was active and is now OVERRIDDEN for this "
                "week — your explicit call."
            )
        return "🤖 Autopilot <b>ON</b> — paper orders will be placed." + note

    # ------------------------------------------------------------ accounting

    @staticmethod
    def _week_key(now: datetime) -> str:
        iso = to_prague(now).isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def _resolved(self) -> List[Dict]:
        return [o for o in self.orders if o.get("result_r") is not None]

    def sl_count_today(self, now: datetime) -> int:
        today = to_prague(now).date()
        return sum(
            1
            for o in self.orders
            if o.get("outcome") == "sl"
            and o.get("resolved_at")
            and to_prague(_parse(o["resolved_at"])).date() == today
        )

    def week_r(self, now: datetime) -> float:
        week = self._week_key(now)
        return sum(
            o["result_r"]
            for o in self._resolved()
            if o.get("resolved_at")
            and self._week_key(_parse(o["resolved_at"])) == week
        )

    def _day_stopped(self, now: datetime) -> bool:
        return self.sl_count_today(now) >= self.cfg.max_sl_day

    def _week_killed(self, now: datetime) -> bool:
        if self.db.kv_get(KV_KILL_OVERRIDE) == self._week_key(now):
            return False
        return self.week_r(now) <= -self.cfg.week_kill_r

    def active_orders(self, pair: Optional[str] = None) -> List[Dict]:
        return [
            o
            for o in self.orders
            if o["status"] in ACTIVE_STATUSES
            and (pair is None or o["pair"] == pair)
        ]

    def active_pairs(self) -> List[str]:
        return sorted({o["pair"] for o in self.active_orders()})

    # ------------------------------------------------------------- placement

    def on_signal(
        self, signal: Dict, result: AnalysisResult, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Place a paper order for a freshly recorded signal, or refuse.

        Returns the Telegram message to send (placement or a risk-gate
        alarm), or None when nothing should be said (silent skips are
        logged). Never raises — the caller treats the autopilot as strictly
        optional next to the alert path.
        """
        try:
            return self._on_signal(signal, result, now)
        except Exception as e:  # noqa: BLE001 — must never break alerts
            logger.error(
                "Autopilot placement failed",
                signal=signal.get("id"),
                error=str(e),
                exc_info=True,
            )
            return None

    def _on_signal(
        self, signal: Dict, result: AnalysisResult, now: Optional[datetime]
    ) -> Optional[str]:
        now = now or datetime.now(tz=timezone.utc)
        pair = signal["pair"]
        if pair not in self.cfg.pairs:
            return None
        if get_instrument(pair).source != "crypto":
            # v1 (D21): the cost model is Binance futures; a forex pair in
            # SMC_AUTO_PAIRS is a configuration mistake, not a trade.
            logger.warning("Autopilot v1 trades crypto only — skipping", pair=pair)
            return None
        if not self.enabled:
            logger.info("Autopilot disabled — signal not traded", id=signal["id"])
            return None
        if self.cfg.tier == "star" and signal.get("tier") != "star":
            logger.info("Autopilot tier=star — regular setup skipped",
                        id=signal["id"])
            return None
        if self._day_stopped(now):
            logger.info("Autopilot day-stop active — signal not traded",
                        id=signal["id"])
            return None
        if self._week_killed(now):
            logger.info("Autopilot weekly kill active — signal not traded",
                        id=signal["id"])
            return None
        if len(self.active_orders(pair)) >= self.cfg.max_open:
            logger.info("Autopilot max_open reached — signal not traded",
                        pair=pair, id=signal["id"])
            return None

        entry = signal["entry"]
        stop_loss = signal["stop_loss"]
        hybrid = bool(signal.get("tp1")) and signal.get("zone_kind") != "RANGE"
        if not hybrid and signal.get("take_profit") is None:
            # No structural objective and no hybrid ladder: nothing to manage
            # toward — a position that could only stop out or time out.
            logger.info("Autopilot: no objective — signal not traded",
                        id=signal["id"])
            return None

        market = bool(result.setup and result.setup.entry_is_market)
        if market:
            base = result.price or entry
            fill_price = _slip(
                signal["direction"] == "long",
                base,
                self.cfg.slippage,
                exiting=False,
            )
            risk_dist = abs(fill_price - stop_loss)
        else:
            fill_price = None
            risk_dist = abs(entry - stop_loss)
        if risk_dist <= 0:
            logger.warning("Autopilot: degenerate risk distance — skipped",
                           id=signal["id"])
            return None

        equity = self.equity
        if equity <= 0:
            logger.error("Autopilot: paper equity depleted — nothing placed")
            return None
        risk_usd = equity * self.cfg.risk_pct / 100.0
        qty = risk_usd / risk_dist

        order: Dict = {
            "id": signal["id"],
            "pair": pair,
            "direction": signal["direction"],
            "tier": signal.get("tier"),
            "zone_kind": signal.get("zone_kind"),
            "qty": qty,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": signal.get("take_profit"),
            "tp1": signal.get("tp1") if hybrid else None,
            "runner_tp": signal.get("runner_tp") if hybrid else None,
            "risk_usd": risk_usd,
            "risk_dist": risk_dist,
            "status": "pending",
            "outcome": None,
            "created_at": signal["created_at"],
            "expires_at": signal.get("expires_at"),
            "filled_at": None,
            "tp1_at": None,
            "resolved_at": None,
            "checked_until": None,
            "fill_price": None,
            "fees_usd": 0.0,
            "pnl_usd": 0.0,
            "result_r": None,
            "equity_after": None,
        }

        fee_note = ""
        if market:
            fee_rate = self.cfg.taker_fee
            fee = qty * fill_price * fee_rate
            order["status"] = "open"
            order["fill_price"] = fill_price
            order["filled_at"] = now.isoformat()
            order["fees_usd"] = fee
            order["pnl_usd"] = -fee
            self._apply_equity(-fee)
            fee_note = f"\nfilled at market {self._fmt(pair, fill_price)}"

        self.orders.append(order)
        self._persist(order)
        logger.info(
            "Autopilot paper order placed",
            id=order["id"], pair=pair, qty=qty, market=market,
        )
        return self._placement_text(order, equity) + fee_note

    # -------------------------------------------------------------- advance

    def advance(
        self, pair: str, candles: List[Candle], now: Optional[datetime] = None
    ) -> List[str]:
        """Advance this pair's active orders; returns Telegram messages.

        Also the place where the D19 day-stop and weekly kill TRIGGER: they
        are consequences of realized outcomes, so they are checked right
        after outcomes realize.
        """
        now = now or datetime.now(tz=timezone.utc)
        messages: List[str] = []
        try:
            for order in list(self.orders):
                if order["pair"] != pair or order["status"] not in ACTIVE_STATUSES:
                    continue
                _, events = evaluate_order(order, candles, now, self.cfg)
                for kind, payload in events:
                    delta = payload.get("equity_delta") or 0.0
                    equity = self._apply_equity(delta) if delta else self.equity
                    if order["status"] in ("closed", "cancelled"):
                        order["equity_after"] = equity
                    messages.append(self._event_text(order, kind, payload, now))
                self._persist(order)
            messages.extend(self._risk_sweep(now))
        except Exception as e:  # noqa: BLE001 — must never break the cycle
            logger.error("Autopilot advance failed", pair=pair, error=str(e),
                         exc_info=True)
        return [m for m in messages if m]

    def _risk_sweep(self, now: datetime) -> List[str]:
        """Fire day-stop / weekly-kill side effects once per day/week."""
        messages: List[str] = []
        if self._day_stopped(now):
            today = to_prague(now).date().isoformat()
            if self.db.kv_get(KV_DAY_STOP_NOTIFIED) != today:
                self.db.kv_set(KV_DAY_STOP_NOTIFIED, today)
                cancelled = self._cancel_pending("day_stop", now)
                messages.append(
                    "🛑 <b>Autopilot day-stop</b>: "
                    f"{self.sl_count_today(now)} realized stop-losses today — "
                    "no new paper orders until tomorrow (Rule 0.2, D19)."
                    + (f" Cancelled {cancelled} pending." if cancelled else "")
                )
        if self._week_killed(now):
            week = self._week_key(now)
            if self.db.kv_get(KV_KILL_NOTIFIED) != week:
                self.db.kv_set(KV_KILL_NOTIFIED, week)
                cancelled = self._cancel_pending("week_kill", now)
                messages.append(
                    "🛑 <b>Autopilot weekly kill</b>: week net "
                    f"{self.week_r(now):+.1f}R ≤ -{self.cfg.week_kill_r:.0f}R "
                    "— no new paper orders this week. /auto on to override."
                    + (f" Cancelled {cancelled} pending." if cancelled else "")
                )
        return messages

    def _cancel_pending(self, outcome: str, now: datetime) -> int:
        count = 0
        for order in self.orders:
            if order["status"] == "pending":
                order["status"] = "cancelled"
                order["outcome"] = outcome
                order["resolved_at"] = now.isoformat()
                self._persist(order)
                count += 1
        return count

    # ------------------------------------------------------------ formatting

    @staticmethod
    def _fmt(pair: str, price: Optional[float]) -> str:
        if price is None:
            return "—"
        return f"{price:.{get_instrument(pair).price_decimals}f}"

    def _placement_text(self, order: Dict, equity: float) -> str:
        pair = escape_html(order["pair"])
        d = order["direction"].upper()
        star = "⭐ " if order.get("tier") == "star" else ""
        f = lambda p: self._fmt(order["pair"], p)  # noqa: E731
        if order["tp1"]:
            objective = (
                f"TP1 {f(order['tp1'])} (half, SL→BE) | "
                f"runner {f(order['runner_tp'])}"
            )
        else:
            label = (
                "opposite boundary"
                if order.get("zone_kind") == "RANGE"
                else "objective"
            )
            objective = f"TP {f(order['take_profit'])} ({label})"
        expires = (
            f"\nexpires {prague_hhmm(_parse(order['expires_at']))} Prague "
            "(Rule 10)"
            if order.get("expires_at")
            else ""
        )
        return (
            f"🤖 {star}<b>Paper order — {pair} {escape_html(d)}</b>\n"
            f"limit {f(order['entry'])} | SL {f(order['stop_loss'])} | "
            f"{objective}\n"
            f"qty {order['qty']:.4f} | risk ${order['risk_usd']:.2f} "
            f"({self.cfg.risk_pct:g}% of ${equity:.2f} paper)"
            f"{expires}"
        )

    def _event_text(
        self, order: Dict, kind: str, payload: Dict, now: datetime
    ) -> Optional[str]:
        pair = escape_html(order["pair"])
        f = lambda p: self._fmt(order["pair"], p)  # noqa: E731
        if kind == "filled":
            return f"🤖 {pair}: paper order filled at {f(payload['price'])}"
        if kind == "tp1":
            return (
                f"🤖 {pair}: TP1 hit at {f(payload['price'])} — half closed "
                f"{payload['equity_delta']:+.2f}$, stop → break-even"
            )
        if kind == "cancelled":
            return (
                f"🤖 {pair}: paper order expired with the session — not filled"
            )
        if kind == "closed":
            outcome = payload["outcome"]
            icons = {
                "sl": "🛑", "tp": "🎯", "tp1_be": "⚖️",
                "tp1_runner": "🏁", "timeout": "⌛",
            }
            names = {
                "sl": "stopped out",
                "tp": "take-profit hit",
                "tp1_be": "runner stopped at break-even (TP1 banked)",
                "tp1_runner": "runner target hit",
                "timeout": "closed at market (5-day timeout)",
            }
            r = order.get("result_r")
            r_text = f"{r:+.2f}R net" if r is not None else "—"
            return (
                f"{icons.get(outcome, '🤖')} {pair}: {names.get(outcome, outcome)}"
                f" — {order['pnl_usd']:+.2f}$ ({r_text})\n"
                f"paper equity ${self.equity:.2f} | week "
                f"{self.week_r(now):+.1f}R | today SL "
                f"{self.sl_count_today(now)}/{self.cfg.max_sl_day}"
            )
        return None

    # ---------------------------------------------------------------- status

    def status_lines(self, now: Optional[datetime] = None) -> List[str]:
        now = now or datetime.now(tz=timezone.utc)
        active = self.active_orders()
        pending = sum(1 for o in active if o["status"] == "pending")
        state = "ON" if self.enabled else "OFF"
        if self._week_killed(now):
            state += " · WEEK-KILLED"
        elif self._day_stopped(now):
            state += " · DAY-STOPPED"
        lines = [
            f"🤖 Autopilot (paper): {state} | equity ${self.equity:.2f}",
            f"   pairs: {', '.join(escape_html(p) for p in self.cfg.pairs)} | "
            f"tier: {escape_html(self.cfg.tier)} | "
            f"active: {len(active) - pending} open / {pending} pending | "
            f"today SL {self.sl_count_today(now)}/{self.cfg.max_sl_day} | "
            f"week {self.week_r(now):+.1f}R",
        ]
        return lines

    def report_text(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(tz=timezone.utc)
        resolved = self._resolved()
        placed = len(self.orders)
        if not placed:
            return (
                "🤖 Autopilot (paper): no orders yet.\n"
                + "\n".join(self.status_lines(now))
            )
        filled = [o for o in self.orders if o.get("filled_at")]
        expired = sum(1 for o in self.orders if o.get("outcome") == "expired")
        decided = len(filled) + expired  # orders whose fill question resolved
        total_r = sum(o["result_r"] for o in resolved)
        total_pnl = sum(o.get("pnl_usd") or 0.0 for o in resolved)
        fees = sum(o.get("fees_usd") or 0.0 for o in self.orders)
        lines = [
            "🤖 <b>Autopilot — paper track</b>",
            f"Orders: {placed} placed | {len(filled)} filled | "
            f"{expired} expired unfilled"
            + (f" (fill rate {len(filled) / decided * 100:.0f}%)"
               if decided else ""),
            f"Resolved: {len(resolved)} | net {total_r:+.1f}R | "
            f"{total_pnl:+.2f}$ | fees paid ${fees:.2f}",
            f"Equity: ${self.equity:.2f} (start ${self.cfg.deposit:.2f})",
        ]
        for tier in ("star", "regular"):
            group = [o for o in resolved if o.get("tier") == tier]
            if group:
                wins = sum(1 for o in group if (o["result_r"] or 0) > 0)
                tier_r = sum(o["result_r"] for o in group)
                label = "⭐ star" if tier == "star" else "regular"
                lines.append(
                    f"• {label}: {len(group)} resolved, {wins}W, {tier_r:+.1f}R"
                )
        outcomes = [o.get("outcome") for o in resolved]
        if outcomes:
            icons = {"sl": "🟥", "tp": "🟩", "tp1_be": "🟨",
                     "tp1_runner": "🟩", "timeout": "⬜"}
            strip = "".join(icons.get(o, "⬜") for o in outcomes[-10:])
            lines.append(f"Last outcomes: {strip}")
        lines.append("")
        lines.extend(self.status_lines(now))
        lines.append(
            "Paper fills: strict trade-through, maker/taker fees, "
            "slippage on stops. The journal (/stats) stays the cost-free "
            "model track."
        )
        return "\n".join(lines)
