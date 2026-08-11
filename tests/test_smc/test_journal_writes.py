"""Regression tests for review-hardening Task 6: journal mutations must
write only the row(s) that actually changed, not the whole in-memory list.

Before this fix, `record`/`mark_taken`/`attach_message`/`update_pair` all
called `SignalJournal.save()`, which re-upserts every signal ever recorded
-- one DB transaction per row, every 5-minute cycle, forever. This pins the
per-row persist: `mark_taken` on one signal among several must touch the DB
exactly once, and `update_pair` on one pair must not touch another pair's
untouched rows.
"""

from datetime import datetime, timezone

from app.services.smc.db import Database
from app.services.smc.journal import SignalJournal
from app.services.smc.models import AnalysisResult, Direction, FVG, TradeSetup, Verdict

# Relative to the real wall clock, not a fixed calendar date: `update_pair`
# evaluates OPEN_TIMEOUT (5 days, journal.py) against `datetime.now()` at
# call time, so a hardcoded `created_at` goes stale and starts flipping
# "open" signals to "timeout" the moment the real clock passes it by 5 days.
NOW = datetime.now(tz=timezone.utc)


def _approved_result(symbol="ETHUSD"):
    result = AnalysisResult(
        symbol=symbol, verdict=Verdict.APPROVED_LIMIT, checked_at=NOW,
    )
    result.session_name = "New York"
    result.setup = TradeSetup(
        direction=Direction.LONG, entry=105.0, stop_loss=95.0,
        take_profit=125.0, rr=2.0,
        fvg=FVG(9, 103.0, 105.0, True, NOW),
    )
    return result


def _seed_signal(journal, pair="ETHUSD", status="pending", taken=None):
    """Insert a signal directly (bypassing `record`) so the spy set up after
    seeding only observes the mutation under test."""
    signal = {
        "id": f"sig-{len(journal.signals)}-{pair}",
        "pair": pair,
        "direction": "long",
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "rr": 2.0,
        "session": "New York",
        "created_at": NOW.isoformat(),
        "expires_at": None,
        "status": status,
        "filled_at": None,
        "resolved_at": None,
        "checked_until": None,
        "taken": taken,
        "message_id": None,
        "alert_text": None,
        "profile_key": "conservative",
    }
    journal.signals.append(signal)
    journal.db.signal_upsert(signal)  # persist the seed directly, not via save()
    return signal


def _spy(db):
    """Wrap db.signal_upsert to record every call, returning the call list."""
    calls = []
    original = db.signal_upsert

    def wrapper(signal):
        calls.append(signal["id"])
        return original(signal)

    db.signal_upsert = wrapper
    return calls


class TestPerRowWrites:
    def test_record_writes_exactly_one_row(self, tmp_path):
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        calls = _spy(db)
        journal.record(_approved_result())
        assert len(calls) == 1

    def test_mark_taken_writes_exactly_one_row_not_len_signals(self, tmp_path):
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        s1 = _seed_signal(journal, pair="ETHUSD")
        _seed_signal(journal, pair="USDJPY")
        _seed_signal(journal, pair="EURUSD")
        assert len(journal.signals) == 3

        calls = _spy(db)
        journal.mark_taken(s1["id"], True)

        assert calls == [s1["id"]]  # exactly one write, not len(signals) == 3

    def test_attach_message_writes_exactly_one_row(self, tmp_path):
        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        s1 = _seed_signal(journal, pair="ETHUSD")
        _seed_signal(journal, pair="USDJPY")

        calls = _spy(db)
        journal.attach_message(s1["id"], 42, "alert text")

        assert calls == [s1["id"]]

    def test_update_pair_does_not_touch_another_pairs_rows(self, tmp_path):
        from app.services.smc.models import Candle

        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        eth = _seed_signal(journal, pair="ETHUSD", status="pending")
        other = _seed_signal(journal, pair="USDJPY", status="pending")

        calls = _spy(db)
        candles = [
            Candle(
                timestamp=NOW, open=99.0, high=101.0, low=98.0, close=100.5,
            )
        ]
        journal.update_pair("ETHUSD", candles)

        assert eth["id"] in calls
        assert other["id"] not in calls  # untouched pair's row is never written

    def test_update_pair_persists_a_resolved_row(self, tmp_path):
        from app.services.smc.models import Candle

        db = Database(str(tmp_path / "j.db"))
        journal = SignalJournal(db)
        signal = _seed_signal(journal, pair="ETHUSD", status="pending")

        calls = _spy(db)
        # entry 100.0 -> this candle's low touches it and fills the signal
        candles = [
            Candle(
                timestamp=NOW, open=101.0, high=101.5, low=99.0, close=100.5,
            )
        ]
        journal.update_pair("ETHUSD", candles)

        assert calls.count(signal["id"]) >= 1
        reloaded = SignalJournal(db)
        assert reloaded.get(signal["id"])["status"] == "open"
