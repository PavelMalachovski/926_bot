# Quiet Zone Alerts (Delivery 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A plan-zone alert fires at most once per zone per session block, and a button under it silences that pair's zone alerts until the block ends.

**Architecture:** `sessions.py` gains the session-block identity and the mute deadline (it is already the single source of truth for the trading day). `WatcherState` stores a list of pinged zones per pair keyed by block id, plus a per-pair mute deadline. `_maybe_plan_zone_alert` drops the "price is still inside the zone" episode rule entirely and asks the state whether an overlapping zone already fired in this block. A `zmute_<PAIR>` callback writes the mute.

**Tech Stack:** Python 3.11, pytest (`asyncio_mode=auto`), structlog, httpx, pytz, SQLite kv store.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-16-quiet-zones-and-range-design.md`. Delivery 1 is §1.1–§1.7; decisions D1, D2, D3 apply.
- **All bot-facing text is English.** Comments and docstrings are English too.
- Telegram messages use `parse_mode=HTML`. Any dynamic string embedded in a message MUST go through `notifier.escape_html`. Only `<b>` and `<pre>` are used — add no other tags.
- `sessions.WINDOWS` is the single source of truth for the trading day. Never hardcode `08:00`, `14:00` or `18:30` anywhere else.
- Trading hours are Prague local time, DST-aware via `sessions.to_prague`/`PRAGUE`. All stored timestamps are ISO-8601 **UTC**.
- Quiet mode: do not add messages the owner did not ask for.
- `db.py` must never crash the watcher; kv values must be JSON-serialisable (lists and dicts of floats/strings only — no tuples, no datetimes).
- Run `pytest tests/ -v` and `flake8 app/ tests/ smc_watcher.py` before every commit. Both must be clean.
- Tests are network-free.
- Mute affects **zone alerts only**. Setup alerts (🚨), Rule 0.4 news warnings, Rule 9 warnings, the 07:45 digest and the 07:55/13:55 plan snapshots must remain reachable while a pair is muted.

---

### Task 1: Session block identity and mute deadline

**Files:**
- Modify: `app/services/smc/sessions.py` (append after `same_session`, end of file)
- Test: `tests/test_smc/test_sessions_blocks.py` (create)

**Interfaces:**
- Consumes: `WINDOWS`, `PRAGUE`, `to_prague` (all already in `sessions.py`).
- Produces:
  - `session_block(utc_dt: datetime) -> Optional[str]` — `"2026-08-16/Frankfurt-London"`, or `None` outside trading hours.
  - `mute_deadline(utc_dt: datetime) -> datetime` — timezone-aware UTC instant at which a mute pressed at `utc_dt` expires.
  - `prague_hhmm(utc_dt: datetime) -> str` — `"14:00"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_sessions_blocks.py`:

```python
"""Session block identity and mute deadlines (spec 2026-08-16 §1.1, §1.4)."""

from datetime import datetime, timezone

from app.services.smc.sessions import (
    PRAGUE,
    mute_deadline,
    prague_hhmm,
    session_block,
)


def _utc(y, m, d, hh, mm):
    """Build a UTC instant from a Prague wall-clock time (DST-aware)."""
    local = PRAGUE.localize(datetime(y, m, d, hh, mm))
    return local.astimezone(timezone.utc)


class TestSessionBlock:
    def test_london_block_id(self):
        assert session_block(_utc(2026, 8, 16, 9, 0)) == (
            "2026-08-16/Frankfurt-London"
        )

    def test_ny_block_id(self):
        assert session_block(_utc(2026, 8, 16, 15, 0)) == "2026-08-16/New York"

    def test_blocks_differ_across_the_1400_split(self):
        assert session_block(_utc(2026, 8, 16, 13, 59)) != session_block(
            _utc(2026, 8, 16, 14, 1)
        )

    def test_same_block_all_afternoon(self):
        assert session_block(_utc(2026, 8, 16, 14, 5)) == session_block(
            _utc(2026, 8, 16, 18, 25)
        )

    def test_blocks_differ_across_days(self):
        assert session_block(_utc(2026, 8, 16, 9, 0)) != session_block(
            _utc(2026, 8, 17, 9, 0)
        )

    def test_none_before_the_open(self):
        assert session_block(_utc(2026, 8, 16, 7, 0)) is None

    def test_none_after_the_close(self):
        assert session_block(_utc(2026, 8, 16, 18, 35)) is None


class TestMuteDeadline:
    def test_london_mute_expires_at_the_ny_open(self):
        assert mute_deadline(_utc(2026, 8, 16, 9, 30)) == _utc(2026, 8, 16, 14, 0)

    def test_ny_mute_expires_at_tomorrows_open(self):
        assert mute_deadline(_utc(2026, 8, 16, 15, 30)) == _utc(2026, 8, 17, 8, 0)

    def test_evening_mute_expires_at_tomorrows_open(self):
        assert mute_deadline(_utc(2026, 8, 16, 21, 0)) == _utc(2026, 8, 17, 8, 0)

    def test_pre_dawn_mute_expires_at_todays_open(self):
        assert mute_deadline(_utc(2026, 8, 16, 3, 0)) == _utc(2026, 8, 16, 8, 0)

    def test_deadline_is_utc_aware(self):
        assert mute_deadline(_utc(2026, 8, 16, 9, 30)).tzinfo is not None


class TestPragueHhmm:
    def test_renders_prague_wall_clock(self):
        assert prague_hhmm(_utc(2026, 8, 16, 14, 0)) == "14:00"

    def test_winter_offset(self):
        assert prague_hhmm(_utc(2026, 1, 16, 9, 5)) == "09:05"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_sessions_blocks.py -v
```

Expected: FAIL — `ImportError: cannot import name 'mute_deadline'`.

- [ ] **Step 3: Implement**

Append to `app/services/smc/sessions.py`. Note the import line at the top of the file must become `from datetime import datetime, time, timedelta`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_sessions_blocks.py -v
```

Expected: PASS, 12 tests.

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: all pass, no lint output.

- [ ] **Step 6: Commit**

```bash
git add app/services/smc/sessions.py tests/test_smc/test_sessions_blocks.py
git commit -m "feat: session block identity and mute deadline in sessions.py"
```

---

### Task 2: State — pinged-zone list and per-pair mute

**Files:**
- Modify: `app/services/smc/state.py:52-62` (the `zone_pinged` load), `:87-102` (`save`)
- Test: `tests/test_smc/test_zone_state.py` (create)

**Interfaces:**
- Consumes: `sessions.mute_deadline`, `sessions.prague_hhmm` (Task 1).
- Produces, on `WatcherState`:
  - `zone_pinged: Dict[str, List[list]]` — per pair, a list of `[bottom, top, direction, block_id]`.
  - `zone_muted: Dict[str, str]` — pair -> ISO UTC deadline.
  - `zone_already_pinged(key: str, bottom: float, top: float, direction: str, block_id: str) -> bool`
  - `remember_zone_ping(key: str, bottom: float, top: float, direction: str, block_id: str) -> None`
  - `mute_zone_alerts(key: str, until_utc: datetime) -> str` — returns the Prague `HH:MM` label.
  - `zone_muted_until(key: str, now: Optional[datetime] = None) -> Optional[str]` — Prague `HH:MM` while the mute is live, else `None`.
  - `clear_zone_mutes() -> List[str]` — pairs that were muted, now freed.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_zone_state.py`:

```python
"""zone_pinged (per-block list) and zone_muted plumbing (spec §1.2, §1.4)."""

from datetime import datetime, timedelta, timezone

from app.services.smc.db import Database
from app.services.smc.state import WatcherState

BLOCK = "2026-08-16/Frankfurt-London"
NEXT_BLOCK = "2026-08-16/New York"


def _state(tmp_path, name="t.db"):
    return WatcherState(Database(str(tmp_path / name)))


class TestZonePingedRecords:
    def test_records_and_matches_the_same_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert state.zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "long", BLOCK
        )

    def test_overlapping_zone_counts_as_the_same_zone(self, tmp_path):
        """D2: the plan is recomputed every 5 minutes and bounds drift."""
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert state.zone_already_pinged(
            "ETHUSD", 3135.0, 3141.0, "long", BLOCK
        )

    def test_non_overlapping_zone_is_a_different_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "ETHUSD", 3200.0, 3210.0, "long", BLOCK
        )

    def test_opposite_direction_is_a_different_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "short", BLOCK
        )

    def test_new_block_forgets_the_zone(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "long", NEXT_BLOCK
        )

    def test_other_pairs_are_independent(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert not state.zone_already_pinged(
            "USDCAD", 3131.0, 3138.0, "long", BLOCK
        )

    def test_two_zones_in_one_block_both_remembered(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        state.remember_zone_ping("ETHUSD", 3200.0, 3210.0, "long", BLOCK)
        assert state.zone_already_pinged("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert state.zone_already_pinged("ETHUSD", 3200.0, 3210.0, "long", BLOCK)

    def test_writing_a_new_block_prunes_the_old_entries(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        state.remember_zone_ping("ETHUSD", 3200.0, 3210.0, "long", NEXT_BLOCK)
        assert state.zone_pinged["ETHUSD"] == [
            [3200.0, 3210.0, "long", NEXT_BLOCK]
        ]

    def test_round_trips_through_the_db(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        assert _state(tmp_path).zone_already_pinged(
            "ETHUSD", 3131.0, 3138.0, "long", BLOCK
        )

    def test_legacy_shapes_are_dropped_on_load(self, tmp_path):
        db = Database(str(tmp_path / "t.db"))
        db.kv_set("zone_pinged", {
            "ETHUSD": True,                                  # pre-auto-plan
            "USDJPY": [3131.0, 3138.0, "long", "2026-08-11"],  # flat 4-list
            "USDCAD": [[1.39448, 1.39584, "short", BLOCK]],    # current
        })
        state = WatcherState(db)
        assert "ETHUSD" not in state.zone_pinged
        assert "USDJPY" not in state.zone_pinged
        assert state.zone_pinged["USDCAD"] == [
            [1.39448, 1.39584, "short", BLOCK]
        ]

    def test_set_profile_still_clears_the_key(self, tmp_path):
        state = _state(tmp_path)
        state.remember_zone_ping("ETHUSD", 3131.0, 3138.0, "long", BLOCK)
        state.set_profile("ETHUSD", "conservative")
        assert "ETHUSD" not in state.zone_pinged


class TestZoneMute:
    def test_mute_is_live_before_the_deadline(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        label = state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        assert state.zone_muted_until("USDCAD", now) == label

    def test_mute_expires(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        assert state.zone_muted_until(
            "USDCAD", now + timedelta(hours=3)
        ) is None

    def test_unmuted_pair_reads_none(self, tmp_path):
        state = _state(tmp_path)
        assert state.zone_muted_until("ETHUSD") is None

    def test_poisoned_deadline_reads_as_unmuted(self, tmp_path):
        state = _state(tmp_path)
        state.zone_muted["ETHUSD"] = "not-a-timestamp"
        assert state.zone_muted_until("ETHUSD") is None

    def test_mute_round_trips_through_the_db(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        assert _state(tmp_path).zone_muted_until("USDCAD", now) is not None

    def test_clear_returns_the_freed_pairs(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("USDCAD", now + timedelta(hours=2))
        state.mute_zone_alerts("ETHUSD", now + timedelta(hours=2))
        assert sorted(state.clear_zone_mutes()) == ["ETHUSD", "USDCAD"]
        assert state.zone_muted == {}

    def test_keys_are_upper_cased(self, tmp_path):
        state = _state(tmp_path)
        now = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
        state.mute_zone_alerts("usdcad", now + timedelta(hours=2))
        assert state.zone_muted_until("USDCAD", now) is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_zone_state.py -v
```

Expected: FAIL — `AttributeError: 'WatcherState' object has no attribute 'remember_zone_ping'`.

- [ ] **Step 3: Implement**

In `app/services/smc/state.py`, add to the imports at the top:

```python
from app.services.smc.sessions import prague_hhmm, to_prague
```

Replace the `zone_pinged` load block (currently lines 52-62) with:

```python
        # pair -> [[bottom, top, direction, block_id], ...]: every plan zone
        # already alerted in the CURRENT session block (owner decision
        # 2026-08-16). One alert per zone per block; nothing re-arms it
        # inside the block. Entries from other blocks, and every legacy
        # shape (the pre-auto-plan bool, the flat 4-element record keyed by
        # Prague date), are dropped on load — the key self-heals, no
        # migration.
        raw_pinged = db.kv_get("zone_pinged") or {}
        self.zone_pinged: Dict[str, List[list]] = {
            k: [e for e in v if isinstance(e, list) and len(e) == 4]
            for k, v in raw_pinged.items()
            if isinstance(v, list) and all(isinstance(e, list) for e in v)
        }
        # pair -> ISO UTC deadline: the owner pressed 🔕 under a zone alert
        # and wants no more zone alerts for this pair until then. Zone
        # alerts only — setups, Rule 0.4 and the digest ignore it (D3).
        self.zone_muted: Dict[str, str] = db.kv_get("zone_muted") or {}
```

Add to `save()`, after the `zone_pinged` line:

```python
        self.db.kv_set("zone_muted", self.zone_muted)
```

Add these methods after `zone_was_planned` (before `set_paused`):

```python
    # ------------------------------------------------------- zone alert dedup

    def zone_already_pinged(
        self, key: str, bottom: float, top: float, direction: str, block_id: str
    ) -> bool:
        """Whether an overlapping zone in the same direction already alerted
        in this session block.

        Overlap rather than equality (owner decision 2026-08-16): the plan
        is recomputed every five minutes and a newly confirmed pivot shifts
        a zone by a fraction of a pip, which an exact comparison reads as a
        new zone — that is what sent USDCAD four identical alerts on
        2026-08-13. A genuinely different zone on the same side, one that
        does not touch the alerted one, still gets its own alert.
        """
        low, high = min(bottom, top), max(bottom, top)
        for entry in self.zone_pinged.get(key.upper(), []):
            e_bottom, e_top, e_dir, e_block = entry
            if e_block != block_id or e_dir != direction:
                continue
            e_low, e_high = min(e_bottom, e_top), max(e_bottom, e_top)
            if e_low <= high and low <= e_high:
                return True
        return False

    def remember_zone_ping(
        self, key: str, bottom: float, top: float, direction: str, block_id: str
    ) -> None:
        """Record a sent zone alert, dropping records of earlier blocks."""
        key = key.upper()
        kept = [e for e in self.zone_pinged.get(key, []) if e[3] == block_id]
        kept.append([
            min(bottom, top), max(bottom, top), direction, block_id,
        ])
        self.zone_pinged[key] = kept
        self.save()

    # -------------------------------------------------------- zone alert mute

    def mute_zone_alerts(self, key: str, until_utc: datetime) -> str:
        """Silence this pair's zone alerts until `until_utc`. Returns the
        Prague HH:MM label to show the owner."""
        self.zone_muted[key.upper()] = until_utc.isoformat()
        self.save()
        return prague_hhmm(until_utc)

    def zone_muted_until(
        self, key: str, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Prague HH:MM while this pair's zone-alert mute is live, else None.

        Read-only, like `Watcher._cooldown_left`: a poisoned or expired
        value reads as "not muted" and is cleaned up by `clear_zone_mutes`
        or overwritten by the next press — a status line must never write
        to the DB.
        """
        raw = self.zone_muted.get(key.upper())
        if not raw:
            return None
        try:
            deadline = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline <= (now or datetime.now(tz=timezone.utc)):
            return None
        return prague_hhmm(deadline)

    def clear_zone_mutes(self) -> List[str]:
        """Drop every zone-alert mute; returns the pairs that were muted."""
        freed = sorted(self.zone_muted)
        self.zone_muted = {}
        self.save()
        return freed
```

`to_prague` is imported already; keep the existing import line working by extending it rather than duplicating it.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_zone_state.py -v
```

Expected: PASS, 18 tests.

- [ ] **Step 5: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: `tests/test_smc/test_autoplan.py::TestPlanZoneAlert` fails — `_maybe_plan_zone_alert` still writes the old flat record. That is Task 3's job; do not touch it here. Everything else passes.

If any test **other** than `TestPlanZoneAlert` fails, stop and fix it before committing.

- [ ] **Step 6: Commit**

```bash
git add app/services/smc/state.py tests/test_smc/test_zone_state.py
git commit -m "feat: per-block zone ping records and per-pair zone mute in state"
```

---

### Task 3: Rewrite the zone-alert decision

**Files:**
- Modify: `smc_watcher.py:1240-1291` (`_maybe_plan_zone_alert`)
- Modify: `tests/test_smc/test_autoplan.py:199-220` (`_StubState`), `:553-569` (two obsolete tests)
- Test: `tests/test_smc/test_zone_dedup.py` (create)

**Interfaces:**
- Consumes: `sessions.session_block` (Task 1); `state.zone_already_pinged`, `state.remember_zone_ping`, `state.zone_muted_until` (Task 2).
- Produces: nothing new — `_maybe_plan_zone_alert(key, result)` keeps its signature. Task 4 adds the keyboard argument to the send call.

- [ ] **Step 1: Update the two tests that encode the old rule**

The 2026-08-11 spec said an exit or an overlapping replacement zone starts a new episode. Owner decision 2026-08-16 reverses both. In `tests/test_smc/test_autoplan.py`, replace `test_exit_resets_episode` and `test_plan_dropping_the_zone_resets_episode` with:

```python
    def test_exit_and_reentry_stays_silent(self, monkeypatch):
        """Owner decision 2026-08-16 (reverses spec 2026-08-11 §5): price
        leaving the zone no longer re-arms the alert. This is the exact
        USDCAD 2026-08-13 failure — four identical messages while price
        oscillated on the zone edge."""
        w, r = self._armed_watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        away = _result_with_candles(price=3160.0)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", away))  # left the zone
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))     # re-entry
        assert len(w.notifier.sent) == 1

    def test_overlapping_replacement_zone_stays_silent(self, monkeypatch):
        """A zone whose bounds drifted between plan recomputes is the same
        trading idea (D2), so it does not earn a second alert."""
        w, r = self._armed_watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        w.planbook.update(
            "ETHUSD", _fetched_entry("ETHUSD", [_scenario(bottom=3135.0, top=3141.0)])
        )
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", r))
        assert len(w.notifier.sent) == 1
```

Then extend `_StubState` (line 199) so it uses the real dedup/mute logic rather than a second implementation that could drift:

```python
class _StubState:
    # The zone dedup/mute methods are borrowed from the real WatcherState:
    # they touch nothing but zone_pinged/zone_muted and save(), and a
    # hand-written copy here would silently drift from production.
    from app.services.smc.state import WatcherState as _Real

    zone_already_pinged = _Real.zone_already_pinged
    remember_zone_ping = _Real.remember_zone_ping
    zone_muted_until = _Real.zone_muted_until
    mute_zone_alerts = _Real.mute_zone_alerts
    del _Real

    def __init__(self):
        self.pairs = ["ETHUSD"]
        self.pair_profile = {}
        self.pair_cooldown = {}
        self.zone_pinged = {}
        self.zone_muted = {}
        self.auto_plan_sent = {}
        self.plan_summary = {}
        self.plan_zones = {}
        self.plan_zones_date = ""
        self.paused = False
```

Leave the rest of `_StubState` (its `save` and `remember_plan_zones`) unchanged.

- [ ] **Step 2: Write the new regression tests**

Create `tests/test_smc/test_zone_dedup.py`:

```python
"""One zone alert per session block, plus the mute gate (spec §1.3, §1.4).

The regression under test is USDCAD 2026-08-13: the same zone alerted at
16:20, 16:35, 16:55 and 17:25 Prague because the old rule re-armed on every
exit from the zone.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.services.smc.models import AnalysisResult, Direction, Trend, Verdict
from app.services.smc.plan import PairPlan, PlanScenario
from app.services.smc.planbook import PlanBook, PlanEntry
from app.services.smc.sessions import PRAGUE
from app.services.smc.state import WatcherState
from tests.test_smc.helpers import make_candles


def _utc(hh, mm, day=16):
    return PRAGUE.localize(
        datetime(2026, 8, day, hh, mm)
    ).astimezone(timezone.utc)


def _scenario(direction=Direction.LONG, bottom=3131.0, top=3138.0):
    entry = top if direction == Direction.LONG else bottom
    return PlanScenario(
        direction=direction, entry=entry, stop_loss=bottom - 2.0,
        take_profit=top + 20.0, rr=2.1, zone_bottom=bottom, zone_top=top,
        speculative=False,
    )


def _entry(scenarios=None):
    plan = PairPlan(pair="ETHUSD", price=3160.0, price_decimals=2,
                    h4_trend=Trend.UP, scenarios=scenarios or [_scenario()])
    return PlanEntry(plan=plan, data={"h4": [], "h1": [], "m5": []},
                     as_of="09:00")


class _State:
    zone_already_pinged = WatcherState.zone_already_pinged
    remember_zone_ping = WatcherState.remember_zone_ping
    zone_muted_until = WatcherState.zone_muted_until
    mute_zone_alerts = WatcherState.mute_zone_alerts

    def __init__(self):
        self.zone_pinged = {}
        self.zone_muted = {}
        self.pair_cooldown = {}
        self.pair_profile = {}

    def save(self):
        pass


class _Notifier:
    def __init__(self):
        self.sent = []
        self.fail_sends = False

    async def send(self, text, reply_markup=None, disable_notification=False):
        if self.fail_sends:
            return None
        self.sent.append((text, reply_markup))
        return len(self.sent)


def _watcher(monkeypatch):
    from smc_watcher import Watcher

    monkeypatch.setattr(settings.smc, "zone_ping", True)
    w = Watcher.__new__(Watcher)
    w.state = _State()
    w.notifier = _Notifier()
    w.planbook = PlanBook()
    w.planbook.update("ETHUSD", _entry())
    return w


def _result(price=3137.0, at=None, verdict=Verdict.WATCH):
    r = AnalysisResult(symbol="ETHUSD", verdict=verdict,
                       checked_at=at or _utc(9, 0), price_decimals=2)
    r.session_name = "Frankfurt/London"
    r.m5_candles = make_candles([price], step_minutes=5)
    return r


class TestOneAlertPerBlock:
    def test_first_touch_alerts(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        assert len(w.notifier.sent) == 1

    def test_exit_and_reentry_within_the_block_is_silent(self, monkeypatch):
        """The USDCAD 2026-08-13 regression."""
        w = _watcher(monkeypatch)
        for minute, price in ((20, 3137.0), (25, 3160.0), (35, 3137.0),
                              (55, 3160.0), (85, 3137.0)):
            asyncio.run(w._maybe_plan_zone_alert(
                "ETHUSD", _result(price=price, at=_utc(9, 0) + timedelta(minutes=minute))
            ))
        assert len(w.notifier.sent) == 1

    def test_drifting_bounds_stay_silent(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        w.planbook.update("ETHUSD", _entry([_scenario(bottom=3131.5, top=3138.5)]))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        assert len(w.notifier.sent) == 1

    def test_new_block_allows_one_more(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result(at=_utc(13, 55))))
        ny = _result(at=_utc(14, 5))
        ny.session_name = "New York"
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", ny))
        assert len(w.notifier.sent) == 2

    def test_non_overlapping_zone_alerts_again(self, monkeypatch):
        w = _watcher(monkeypatch)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        w.planbook.update("ETHUSD", _entry([_scenario(bottom=3200.0, top=3210.0)]))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result(price=3205.0)))
        assert len(w.notifier.sent) == 2

    def test_send_failure_retries_next_cycle(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.notifier.fail_sends = True
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        w.notifier.fail_sends = False
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        assert len(w.notifier.sent) == 1


class TestMuteGate:
    def test_muted_pair_gets_no_zone_alert(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result(at=_utc(9, 30))))
        assert w.notifier.sent == []

    def test_expired_mute_lets_the_alert_through(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        ny = _result(at=_utc(14, 5))
        ny.session_name = "New York"
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", ny))
        assert len(w.notifier.sent) == 1

    def test_mute_of_one_pair_does_not_silence_another(self, monkeypatch):
        w = _watcher(monkeypatch)
        w.planbook.update("USDCAD", _entry())
        w.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        asyncio.run(w._maybe_plan_zone_alert("USDCAD", _result(at=_utc(9, 30))))
        assert len(w.notifier.sent) == 1
```

- [ ] **Step 3: Run the new tests to verify they fail**

```bash
pytest tests/test_smc/test_zone_dedup.py -v
```

Expected: FAIL — `test_exit_and_reentry_within_the_block_is_silent` reports 3 sends, and the mute tests send anyway. `test_first_touch_alerts` may already pass; that is fine.

- [ ] **Step 4: Rewrite `_maybe_plan_zone_alert`**

Add `session_block` to the `sessions` import in `smc_watcher.py` (it already imports `active_session` and `to_prague` from there). Replace the whole body of `_maybe_plan_zone_alert` (`smc_watcher.py:1240-1291`) with:

```python
    async def _maybe_plan_zone_alert(
        self, key: str, result: Optional[AnalysisResult]
    ) -> None:
        """Price entered a zone the CURRENT plan names: one alert per zone
        per session block (owner decision 2026-08-16, spec §1.3), carrying
        the plan's projected bracket.

        The 2026-08-11 "episode" rule is gone. It kept the alert armed as
        soon as the last closed M5 candle stopped overlapping the zone, so
        price oscillating on a zone edge re-alerted every few cycles —
        USDCAD sent the identical message four times on 2026-08-13. Silence
        now lasts the whole block and survives exits, re-entries and the
        five-minute plan recompute; the owner can end it early only by the
        block ending, or extend it with the 🔕 button.
        """
        if not settings.smc.zone_ping or result is None:
            return
        if not result.session_name or not result.m5_candles:
            return  # Rule 0.1: get-ready alerts belong to the session
        block = session_block(result.checked_at)
        if block is None:
            return
        if self.state.zone_muted_until(key, result.checked_at):
            return  # the owner silenced this pair's zone alerts (D3)
        if result.verdict in APPROVED:
            return  # the full 🚨 alert covers this touch
        last = result.m5_candles[-1]
        scenario = self.planbook.scenario_for_touch(key, last.low, last.high)
        if scenario is None:
            return
        if self._cooldown_left(key):
            return  # already managing a position here
        if self.state.zone_already_pinged(
            key, scenario.zone_bottom, scenario.zone_top,
            scenario.direction.value, block,
        ):
            return
        sent = await self.notifier.send(
            format_zone_alert(key, scenario, result.price_decimals)
        )
        if sent:
            # mark AFTER the send: a failed delivery must retry next cycle
            self.state.remember_zone_ping(
                key, scenario.zone_bottom, scenario.zone_top,
                scenario.direction.value, block,
            )
            logger.info("Plan-zone alert sent", pair=key, block=block)
```

Note `WatcherState._prague_day` is no longer called here; leave the method itself alone, `remember_plan_zones` still uses it.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_zone_dedup.py tests/test_smc/test_autoplan.py -v
```

Expected: PASS, including the two rewritten `TestPlanZoneAlert` tests.

- [ ] **Step 6: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: all pass, no lint output.

- [ ] **Step 7: Commit**

```bash
git add smc_watcher.py tests/test_smc/test_zone_dedup.py tests/test_smc/test_autoplan.py
git commit -m "fix: one zone alert per session block, no re-arm on zone exit"
```

---

### Task 4: The 🔕 mute button

**Files:**
- Modify: `app/services/smc/notifier.py` (after `format_zone_alert`, line 538)
- Modify: `app/services/smc/telegram_bot.py:57-87` (constructor), `:401` (`_handle_callback`)
- Modify: `smc_watcher.py:297-309` (bot wiring), `_maybe_plan_zone_alert` (the send call)
- Test: `tests/test_smc/test_zone_mute_button.py` (create)

**Interfaces:**
- Consumes: `sessions.mute_deadline`, `sessions.prague_hhmm` (Task 1); `state.mute_zone_alerts` (Task 2).
- Produces:
  - `notifier.zone_alert_keyboard(pair: str, until_hhmm: str) -> dict`
  - `TelegramCommandBot(..., on_zone_mute: Optional[Callable[[str], Awaitable[str]]] = None)` — called with the pair key, returns **only** the Prague `HH:MM` deadline label. The bot composes both the callback answer and the button label from it, so neither has to parse a sentence.
  - `Watcher.mark_zone_mute(key: str) -> str` — returns that same `HH:MM` label.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_smc/test_zone_mute_button.py`:

```python
"""The 🔕 button under a zone alert: keyboard, callback, watcher hook."""

import asyncio
from datetime import datetime, timezone

from app.core.config import settings
from app.services.smc.notifier import zone_alert_keyboard
from app.services.smc.sessions import PRAGUE
from app.services.smc.state import WatcherState
from app.services.smc.telegram_bot import TelegramCommandBot


def _utc(hh, mm, day=16):
    return PRAGUE.localize(datetime(2026, 8, day, hh, mm)).astimezone(
        timezone.utc
    )


class TestZoneAlertKeyboard:
    def test_single_mute_button(self):
        kb = zone_alert_keyboard("USDCAD", "14:00")
        assert kb == {"inline_keyboard": [[{
            "text": "🔕 Mute USDCAD zone alerts till 14:00",
            "callback_data": "zmute_USDCAD",
        }]]}


class _State:
    mute_zone_alerts = WatcherState.mute_zone_alerts
    zone_muted_until = WatcherState.zone_muted_until

    def __init__(self):
        self.zone_muted = {}
        self.paused = False

    def save(self):
        pass


class _Bot(TelegramCommandBot):
    def __init__(self, on_zone_mute):
        self.calls = []
        self.state = _State()
        self.owner_chat_id = "1"
        self.on_zone_mute = on_zone_mute
        self.trade_journal = None
        self.on_trade_mark = None
        self.on_plan = None
        self.on_stored_plan = None

    async def _api(self, method, **payload):
        self.calls.append((method, payload))
        return {}


class TestZmuteCallback:
    def _callback(self):
        return {
            "id": "cb1",
            "data": "zmute_USDCAD",
            "message": {"chat": {"id": 1}, "message_id": 42},
        }

    def test_calls_the_hook_and_answers(self):
        async def hook(key):
            return "14:00"

        bot = _Bot(hook)
        asyncio.run(bot._handle_callback(self._callback()))
        answered = [c for c in bot.calls if c[0] == "answerCallbackQuery"]
        assert answered[0][1]["text"] == "USDCAD zone alerts muted till 14:00"

    def test_replaces_the_keyboard_with_an_inert_button(self):
        async def hook(key):
            return "14:00"

        bot = _Bot(hook)
        asyncio.run(bot._handle_callback(self._callback()))
        edits = [c for c in bot.calls if c[0] == "editMessageReplyMarkup"]
        assert edits[0][1]["reply_markup"] == {"inline_keyboard": [[{
            "text": "🔕 Muted till 14:00", "callback_data": "noop",
        }]]}


class TestWatcherHook:
    def _watcher(self, monkeypatch):
        from smc_watcher import Watcher

        w = Watcher.__new__(Watcher)
        w.state = _State()
        return w

    def test_mute_hook_stores_the_block_deadline(self, monkeypatch):
        import smc_watcher as mod

        w = self._watcher(monkeypatch)
        monkeypatch.setattr(mod, "mute_deadline", lambda now: _utc(14, 0))
        assert asyncio.run(w.mark_zone_mute("USDCAD")) == "14:00"
        assert w.state.zone_muted_until("USDCAD", _utc(9, 30)) == "14:00"


class TestAlertCarriesTheButton:
    def test_zone_alert_is_sent_with_the_mute_keyboard(self, monkeypatch):
        from tests.test_smc.test_zone_dedup import _result, _watcher

        w = _watcher(monkeypatch)
        monkeypatch.setattr(settings.smc, "zone_ping", True)
        asyncio.run(w._maybe_plan_zone_alert("ETHUSD", _result()))
        _text, markup = w.notifier.sent[0]
        assert markup["inline_keyboard"][0][0]["callback_data"] == "zmute_ETHUSD"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_zone_mute_button.py -v
```

Expected: FAIL — `ImportError: cannot import name 'zone_alert_keyboard'`.

- [ ] **Step 3: Add the keyboard**

In `app/services/smc/notifier.py`, after `format_zone_alert`:

```python
def zone_alert_keyboard(pair: str, until_hhmm: str) -> dict:
    """The 🔕 button under a zone alert (owner decision 2026-08-16).

    Silences this pair's ZONE alerts only — setup alerts, Rule 0.4 news
    warnings and the digest are unaffected (D3). The label shows the
    deadline that applies at send time; the authoritative deadline is
    recomputed when the button is actually pressed.
    """
    return {"inline_keyboard": [[{
        "text": f"🔕 Mute {pair} zone alerts till {until_hhmm}",
        "callback_data": f"zmute_{pair}",
    }]]}
```

`pair` is an instrument key from our own registry (`INSTRUMENTS`), never owner input, so it needs no `escape_html` — and button labels are not HTML-parsed in any case.

- [ ] **Step 4: Wire the callback**

In `app/services/smc/telegram_bot.py`, add the parameter to `__init__` after `on_stored_plan` (both the signature and the assignment):

```python
        on_zone_mute: Optional[Callable[[str], Awaitable[str]]] = None,
```
```python
        self.on_zone_mute = on_zone_mute
```

In `_handle_callback`, add this branch immediately before the `if data.startswith("aplan_")` branch:

```python
        if data.startswith("zmute_") and self.on_zone_mute:
            key = data[len("zmute_"):]
            # The hook returns the Prague HH:MM deadline and nothing else,
            # so neither string below has to be parsed back out of a
            # sentence.
            until = await self.on_zone_mute(key)
            answer["text"] = f"{key} zone alerts muted till {until}"
            message = callback.get("message", {})
            if message:
                await self._api(
                    "editMessageReplyMarkup",
                    chat_id=message["chat"]["id"],
                    message_id=message["message_id"],
                    reply_markup={"inline_keyboard": [[{
                        "text": f"🔕 Muted till {until}",
                        "callback_data": "noop",
                    }]]},
                )
            await self._api("answerCallbackQuery", **answer)
            return
```

- [ ] **Step 5: Add the watcher hook and send the keyboard**

In `smc_watcher.py`, extend the `sessions` import with `mute_deadline` and `prague_hhmm`, and add the `zone_alert_keyboard` import from `notifier`.

Add this method next to `mark_trade`:

```python
    async def mark_zone_mute(self, key: str) -> str:
        """🔕 button: silence this pair's zone alerts until the current
        session block ends (or tomorrow's open, in the last block of the
        day). Setup alerts and Rule 0.4 warnings are untouched (D3).

        Returns the Prague HH:MM deadline; the bot builds both the callback
        answer and the replacement button label from it.
        """
        key = key.upper()
        until = self.state.mute_zone_alerts(
            key, mute_deadline(datetime.now(tz=timezone.utc))
        )
        logger.info("Zone alerts muted", pair=key, until=until)
        return until
```

Wire it into the bot constructor (`smc_watcher.py:297-309`), after `on_stored_plan=self.on_stored_plan,`:

```python
            on_zone_mute=self.mark_zone_mute,
```

And attach the keyboard in `_maybe_plan_zone_alert` — replace the `sent = await self.notifier.send(...)` call with:

```python
        sent = await self.notifier.send(
            format_zone_alert(key, scenario, result.price_decimals),
            reply_markup=zone_alert_keyboard(
                key, prague_hhmm(mute_deadline(result.checked_at))
            ),
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_zone_mute_button.py -v
```

Expected: PASS, 6 tests.

- [ ] **Step 7: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: all pass, no lint output.

- [ ] **Step 8: Commit**

```bash
git add app/services/smc/notifier.py app/services/smc/telegram_bot.py smc_watcher.py tests/test_smc/test_zone_mute_button.py
git commit -m "feat: mute button under zone alerts, scoped to the session block"
```

---

### Task 5: `/unmute` and the `/status` line

**Files:**
- Modify: `app/services/smc/telegram_bot.py:32-51` (`HELP_TEXT`), `:236-252` (`setMyCommands`), `:290-355` (`_handle_command`)
- Modify: `smc_watcher.py:1383-1417` (`status_text`)
- Test: `tests/test_smc/test_zone_mute_button.py` (extend)

**Interfaces:**
- Consumes: `state.clear_zone_mutes`, `state.zone_muted_until` (Task 2).
- Produces: the `/unmute` command; a `🔕 Zone alerts muted:` line in `/status`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_smc/test_zone_mute_button.py`:

```python
class _UnmuteState(_State):
    clear_zone_mutes = WatcherState.clear_zone_mutes


class _CommandBot(_Bot):
    def __init__(self):
        super().__init__(None)
        self.state = _UnmuteState()
        self.sent = []

    async def send(self, text, reply_markup=None):
        self.sent.append(text)
        return 1


class TestUnmuteCommand:
    def test_unmute_frees_every_pair(self):
        bot = _CommandBot()
        bot.state.mute_zone_alerts("USDCAD", _utc(14, 0))
        bot.state.mute_zone_alerts("ETHUSD", _utc(14, 0))
        asyncio.run(bot._handle_command("/unmute"))
        assert bot.state.zone_muted == {}
        assert "USDCAD" in bot.sent[0] and "ETHUSD" in bot.sent[0]

    def test_unmute_with_nothing_muted(self):
        bot = _CommandBot()
        asyncio.run(bot._handle_command("/unmute"))
        assert "No pairs are muted" in bot.sent[0]


class TestStatusLine:
    def test_status_lists_muted_pairs(self, monkeypatch):
        from smc_watcher import Watcher

        w = Watcher.__new__(Watcher)
        w.state = _UnmuteState()
        w.state.pairs = ["ETHUSD", "USDCAD"]
        w.state.pair_cooldown = {}
        w.state.notify_level = "all"
        w.last_results = {}
        w.state.mute_zone_alerts("USDCAD", _utc(23, 59))
        assert "🔕 Zone alerts muted: USDCAD (till 23:59)" in w.status_text()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_smc/test_zone_mute_button.py -k "Unmute or StatusLine" -v
```

Expected: FAIL — `/unmute` falls through to "Unknown command", and `status_text` has no mute line.

- [ ] **Step 3: Add the command**

In `app/services/smc/telegram_bot.py`, add to `_handle_command`, immediately after the `/resume` branch:

```python
        elif command == "/unmute":
            freed = self.state.clear_zone_mutes()
            if freed:
                await self.send(
                    "🔔 Zone alerts un-muted for: " + ", ".join(freed)
                )
            else:
                await self.send("No pairs are muted.")
```

Add to `HELP_TEXT`, after the `/resume` line:

```
    "/unmute — un-mute zone alerts for every pair\n"
```

Add to the `setMyCommands` list, after the `resume` entry:

```python
                {
                    "command": "unmute",
                    "description": "Un-mute zone alerts for every pair",
                },
```

- [ ] **Step 4: Add the status line**

In `smc_watcher.py`'s `status_text`, immediately after the existing `🔕 Muted (taken)` block, add:

```python
        zone_muted = [
            f"{k} (till {until})"
            for k in self.state.pairs
            if (until := self.state.zone_muted_until(k))
        ]
        if zone_muted:
            lines.append(f"🔕 Zone alerts muted: {', '.join(zone_muted)}")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_smc/test_zone_mute_button.py -v
```

Expected: PASS, 10 tests.

- [ ] **Step 6: Run the full suite and lint**

```bash
pytest tests/ -q && flake8 app/ tests/ smc_watcher.py
```

Expected: all pass, no lint output.

- [ ] **Step 7: Commit**

```bash
git add app/services/smc/telegram_bot.py smc_watcher.py tests/test_smc/test_zone_mute_button.py
git commit -m "feat: /unmute command and zone-mute line in /status"
```

---

### Task 6: Documentation and pull request

**Files:**
- Modify: `CLAUDE.md` (the "Plan-centric zone alert" bullet in Conventions, and the `state.py` line in the architecture tree)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code-facing.

- [ ] **Step 1: Update the architecture tree line for `state.py`**

Replace:

```
└── state.py              runtime state (pairs, dedup keys) on SQLite kv
```

with:

```
└── state.py              runtime state (pairs, dedup keys, zone-alert
                          mutes) on SQLite kv
```

- [ ] **Step 2: Rewrite the "Plan-centric zone alert" bullet**

Replace the whole existing bullet (it describes the retired episode rule) with:

```markdown
- **Plan-centric zone alert** (spec 2026-08-11 §5, dedup rewritten by owner
  decision 2026-08-16): fires when price touches a zone named by the pair's
  *current* plan (`planbook.scenario_for_touch`), carrying that scenario's
  projected bracket. It fires **once per zone per session block** —
  `sessions.session_block` supplies the block id, and
  `state.zone_pinged[key]` holds every `[bottom, top, direction, block_id]`
  already alerted. Two zones count as the same zone when the direction
  matches and the bounds overlap at all; the old rule (exact bounds, and
  the episode ending as soon as price left the zone) re-armed the alert
  every time price oscillated on a zone edge and sent USDCAD four identical
  messages on 2026-08-13. Nothing re-arms an alert inside a block. The 🔕
  button under the alert writes `state.zone_muted[pair]` (ISO UTC deadline
  from `sessions.mute_deadline`: this block's end, or tomorrow's open in
  the day's last block) and silences that pair's zone alerts only — setup
  alerts, Rule 0.4 warnings and the 07:45 digest ignore it. `/unmute`
  clears every mute; `/status` lists them.
  `state.remember_plan_zones` (the separate "from this morning's plan"
  provenance, spec 2026-08-06 §6) is still written **only** by the
  07:55/13:55 snapshots and manual `/plan` — never by the 5-minute
  recompute.
```

- [ ] **Step 3: Run the full suite and lint one last time**

```bash
pytest tests/ -v && flake8 app/ tests/ smc_watcher.py
```

Expected: all pass, no lint output. Paste the summary line into the PR.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: zone-alert dedup by session block and the mute button"
```

- [ ] **Step 5: Open the pull request**

Follow `.github/pull_request_template.md`. Content:

- **What:** Delivery 1 of the 2026-08-16 spec — plan-zone alerts fire once per zone per session block, and a 🔕 button mutes a pair's zone alerts until the block ends.
- **Why:** USDCAD sent the identical zone alert four times on 2026-08-13 (16:20, 16:35, 16:55, 17:25 Prague) because the episode rule re-armed whenever the last M5 candle left the zone. Owner decisions D1–D3, 2026-08-16.
- **Testing:** `pytest tests/ -v` (paste the count), `flake8` clean. New: `test_sessions_blocks.py`, `test_zone_state.py`, `test_zone_dedup.py`, `test_zone_mute_button.py`. Changed: two `TestPlanZoneAlert` tests that encoded the reversed 2026-08-11 rule.
- **Note:** the `zone_pinged` kv format changes from a flat record to a list of per-block records; legacy values are dropped on load, the key self-heals, no migration. New kv key `zone_muted`.

```bash
gh pr create --base master --title "fix: one zone alert per session block + mute button" --body-file <path to the body>
```

---

## Self-Review

**Spec coverage.** §1.1 → Task 1. §1.2 → Task 2. §1.3 → Task 3. §1.4 → Tasks 2+4. §1.5 → Task 5. §1.6 → the test lists in Tasks 1-5 (every bullet mapped: exit/re-entry → Task 3 `test_exit_and_reentry_within_the_block_is_silent`; drifting bounds → Task 3 `test_drifting_bounds_stay_silent`; block rollover → Task 3 `test_new_block_allows_one_more`; non-overlapping zone → Task 3 `test_non_overlapping_zone_alerts_again`; mute on/expired → Task 3 `TestMuteGate`; mute does not suppress 🚨 → the `APPROVED` early return is checked by the existing `test_approved_verdict_is_covered_by_full_alert`, and mute is scoped to `_maybe_plan_zone_alert` alone, which no setup path calls; `session_block` boundaries → Task 1; legacy `zone_pinged` → Task 2 `test_legacy_shapes_are_dropped_on_load`). §1.7 → the file lists, with `CLAUDE.md` in Task 6.

**Type consistency.** `block_id` is a `str` everywhere. `zone_already_pinged`/`remember_zone_ping` take `(key, bottom, top, direction, block_id)` in that order in Task 2, Task 3 and both stub states. `direction` is always the enum's `.value` string (`"long"`/`"short"`), never the enum. `mute_zone_alerts` takes an aware UTC `datetime` and returns the `HH:MM` label; `zone_muted_until` returns that same label or `None`. `mute_deadline` returns an aware UTC `datetime`, so `prague_hhmm(mute_deadline(...))` is the label everywhere it is rendered.

**Out of scope for this plan:** H1 FVG zones, M5 OB/FVG marking, the H4/H1 disagreement label, and range detection — Deliveries 2 and 3 of the spec, each with its own plan.
