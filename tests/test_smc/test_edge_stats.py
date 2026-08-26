"""The feature vector on every signal, and the expectancy cuts built on it.

Audit finding F4 (2026-08-26): the journal recorded the tier verdict and the
outcome, but nothing about WHY a setup was starred — so "is the pd condition
earning its keep?" was unanswerable no matter how long the bot ran. These pin
what gets recorded, that the columns migrate onto a live database, and that
the /stats cuts refuse to print an average they cannot support.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.smc.db import SIGNAL_COLUMNS, Database
from app.services.smc.journal import (
    MIN_EDGE_SAMPLE,
    TIER_CONDITIONS,
    SignalJournal,
)
from app.services.smc.models import (
    AnalysisResult,
    Direction,
    FVG,
    TradeSetup,
    Trend,
    Verdict,
    Zone,
)
from app.services.smc.sessions import PRAGUE
from app.services.smc import pd as pd_module
from tests.test_smc.helpers import (
    H1_PULLBACK_CLOSES,
    H4_UPTREND_CLOSES,
    make_candles,
)

FEATURE_COLUMNS = (
    "tier_missed", "room_r", "sweep", "entry_gap_r", "pd_pct", "pd_side",
    "pd_basis", "pd_ote", "direction_source", "h4_trend", "h1_trend",
    "entry_hour",
)


def _utc(hh, mm=0, day=26):
    return PRAGUE.localize(datetime(2026, 8, day, hh, mm)).astimezone(
        timezone.utc
    )


def _result(**kw):
    gap = FVG(
        top=3139.5, bottom=3135.0, index=4, is_bullish=True,
        timestamp=_utc(9, 30),
    )
    result = AnalysisResult(
        symbol="ETHUSD",
        verdict=Verdict.APPROVED_LIMIT,
        checked_at=kw.pop("checked_at", _utc(15, 10)),
        price=3150.0,
        h4_trend=kw.pop("h4_trend", Trend.UP),
        h1_trend=kw.pop("h1_trend", Trend.DOWN),
        direction_source=kw.pop("direction_source", "h4"),
        session_name="New York",
        price_decimals=2,
    )
    result.h1_zone = Zone(
        bottom=3131.0, top=3138.0, is_demand=True, pivot_index=0,
        timestamp=_utc(9),
    )
    result.pd = kw.pop("pd", pd_module.read(
        make_candles(H4_UPTREND_CLOSES, step_minutes=240),
        make_candles(H1_PULLBACK_CLOSES, step_minutes=60),
        3139.5, Direction.LONG,
    ))
    result.setup = TradeSetup(
        direction=Direction.LONG, entry=3139.5, stop_loss=3128.0,
        take_profit=3219.0, rr=6.91, fvg=gap,
        tier_star=kw.pop("tier_star", False),
        tier_missed=kw.pop("tier_missed", ["pd", "trend"]),
        room_r=kw.pop("room_r", 2.4),
        sweep=kw.pop("sweep", "PDL"),
        entry_gap_r=kw.pop("entry_gap_r", 0.31),
    )
    assert not kw, kw
    return result


# ------------------------------------------------------------- what is kept


def test_record_writes_the_whole_feature_vector(tmp_path):
    journal = SignalJournal(Database(str(tmp_path / "smc.db")))
    signal = journal.record(_result())

    assert signal["tier_missed"] == "pd,trend"
    assert signal["room_r"] == pytest.approx(2.4)
    assert signal["sweep"] == "PDL"
    assert signal["entry_gap_r"] == pytest.approx(0.31)
    assert signal["pd_side"] == "discount"
    assert signal["pd_basis"] == "H4"
    assert signal["pd_ote"] in (0, 1)
    assert signal["direction_source"] == "h4"
    assert signal["h4_trend"] == "up"
    assert signal["h1_trend"] == "down"
    assert signal["entry_hour"] == 15  # 15:10 Prague, not the UTC hour


def test_unmeasurable_conditions_are_recorded_as_null_not_as_zero(tmp_path):
    """`room_r` is genuinely None when no pool sits ahead, and that is a
    PASSING condition. Storing it as 0.0 would read as "no room at all" —
    the exact opposite — and poison every average built on the column."""
    journal = SignalJournal(Database(str(tmp_path / "smc.db")))
    signal = journal.record(
        _result(room_r=None, sweep=None, pd=None, tier_missed=["sweep"])
    )
    assert signal["room_r"] is None
    assert signal["sweep"] is None
    assert signal["pd_pct"] is None
    assert signal["pd_side"] is None
    assert signal["pd_ote"] is None
    assert signal["tier_missed"] == "sweep"


def test_a_clean_setup_records_an_empty_missed_list_not_null(tmp_path):
    """"" means "measured, nothing missed"; NULL means "never recorded".
    `_edge_lines` keys on exactly that difference to exclude legacy rows."""
    journal = SignalJournal(Database(str(tmp_path / "smc.db")))
    signal = journal.record(_result(tier_missed=[], tier_star=True))
    assert signal["tier_missed"] == ""


def test_the_vector_survives_a_round_trip_through_sqlite(tmp_path):
    db = Database(str(tmp_path / "smc.db"))
    journal = SignalJournal(db)
    written = journal.record(_result())
    reloaded = {s["id"]: s for s in SignalJournal(db).signals}[written["id"]]
    for column in FEATURE_COLUMNS:
        assert reloaded[column] == written[column], column


def test_the_columns_migrate_onto_a_database_that_predates_them(tmp_path):
    """Production DBs are migrated in place (CLAUDE.md) — a fresh schema is
    not enough, the ALTER TABLE list has to carry every new column."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    legacy = sqlite3.connect(path)
    with legacy:
        legacy.execute(
            "CREATE TABLE signals (id TEXT PRIMARY KEY, pair TEXT NOT NULL, "
            "direction TEXT NOT NULL, entry REAL NOT NULL, "
            "stop_loss REAL NOT NULL, take_profit REAL, rr REAL NOT NULL, "
            "session TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO signals VALUES ('old1','ETHUSD','long',100.0,99.0,"
            "103.0,3.0,'New York','2026-08-01T12:00:00+00:00','sl')"
        )
    legacy.close()

    db = Database(path)
    columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(signals)")
    }
    for column in FEATURE_COLUMNS:
        assert column in columns, column
    # The pre-existing row survives, with the new columns empty.
    old = {s["id"]: s for s in SignalJournal(db).signals}["old1"]
    assert old["tier_missed"] is None
    # ...and a new signal writes into the migrated table.
    fresh = SignalJournal(db).record(_result())
    assert fresh["tier_missed"] == "pd,trend"


def test_every_feature_column_is_declared(tmp_path):
    """A column written by `record` but missing from SIGNAL_COLUMNS would be
    silently dropped by the table rebuild (db.py says so out loud)."""
    for column in FEATURE_COLUMNS:
        assert column in SIGNAL_COLUMNS, column


# --------------------------------------------------------------- the cuts


def _row(missed, result_r, **kw):
    row = {c: None for c in SIGNAL_COLUMNS}
    row.update(
        id=f"x{abs(hash((missed, result_r, tuple(sorted(kw.items())))))}"[:10],
        pair=kw.pop("pair", "ETHUSD"), direction="long", entry=100.0,
        stop_loss=99.0, take_profit=103.0, rr=3.0,
        session=kw.pop("session", "New York"),
        created_at=(
            datetime.now(tz=timezone.utc) - timedelta(days=1)
        ).isoformat(),
        status="tp" if result_r > 0 else "sl", taken=1,
        tier="star" if not missed else "regular", result_r=result_r,
        tier_missed=missed, zone_kind=kw.pop("zone_kind", "OB"),
        direction_source=kw.pop("direction_source", "h4"),
        entry_hour=kw.pop("entry_hour", 15),
    )
    row.update(kw)
    return row


def _stats(rows, tmp_path):
    journal = SignalJournal(Database(str(tmp_path / "smc.db")))
    journal.signals = rows
    return journal.stats_text()


def test_condition_cut_splits_missed_from_clean(tmp_path):
    rows = [_row("pd", -1.0) for _ in range(4)]
    rows += [_row("", 2.0) for _ in range(4)]
    text = _stats(rows, tmp_path)
    assert "<b>Edge by condition</b> (8 resolved with data)" in text
    # pd: 4 missed at -1R, 4 clean at +2R
    assert "pd" in text
    assert "4 · -1.00R" in text
    assert "4 · +2.00R" in text
    for name in TIER_CONDITIONS:
        assert name in text


def test_a_thin_cell_shows_its_count_but_withholds_an_average(tmp_path):
    """Two trades are not an edge. The count still prints, so the owner can
    watch the sample grow instead of wondering where the row went."""
    thin = MIN_EDGE_SAMPLE - 1
    rows = [_row("room", -1.0) for _ in range(thin)]
    rows += [_row("", 2.0) for _ in range(4)]
    text = _stats(rows, tmp_path)
    assert f"{thin} · —" in text
    assert f"{thin} · -1.00R" not in text


def test_legacy_rows_without_a_vector_are_left_out(tmp_path):
    """NULL `tier_missed` means "never recorded", not "nothing missed" —
    counting those rows as clean would credit the filters with outcomes
    nobody measured."""
    rows = [_row("", 2.0) for _ in range(3)]
    for row in rows[:2]:
        row["tier_missed"] = None
    text = _stats(rows, tmp_path)
    assert "<b>Edge by condition</b> (1 resolved with data)" in text


def test_the_block_is_absent_until_something_has_been_recorded(tmp_path):
    rows = [_row("", 2.0) for _ in range(3)]
    for row in rows:
        row["tier_missed"] = None
    assert "Edge by condition" not in _stats(rows, tmp_path)


def test_session_and_hour_cuts_appear_once_a_bucket_is_big_enough(tmp_path):
    rows = [
        _row("", 2.0, session="Frankfurt/London", entry_hour=9)
        for _ in range(MIN_EDGE_SAMPLE)
    ]
    rows += [
        _row("pd", -1.0, session="New York", entry_hour=16)
        for _ in range(MIN_EDGE_SAMPLE)
    ]
    text = _stats(rows, tmp_path)
    assert "Edge by session block" in text
    assert "Frankfurt/London" in text and "New York" in text
    assert "Edge by entry hour (Prague)" in text
    assert "09:00" in text and "16:00" in text


def test_a_cut_with_only_thin_buckets_is_not_printed(tmp_path):
    """Eight hours of one trade each is eight lines of nothing."""
    rows = [_row("", 2.0, entry_hour=h) for h in range(9, 17)]
    text = _stats(rows, tmp_path)
    assert "Edge by entry hour" not in text


def test_a_cut_with_a_single_bucket_is_not_printed(tmp_path):
    """One bucket is not a comparison."""
    rows = [_row("", 2.0, zone_kind="OB") for _ in range(6)]
    text = _stats(rows, tmp_path)
    assert "Edge by zone kind" not in text


def test_the_cuts_keep_pre_tags_balanced(tmp_path):
    """Telegram rejects a message with an unclosed tag, and this block opens
    several <pre> sections conditionally."""
    rows = [
        _row("", 2.0, session="Frankfurt/London", zone_kind="OB",
             direction_source="h4", entry_hour=9)
        for _ in range(MIN_EDGE_SAMPLE)
    ]
    rows += [
        _row("pd,room", -1.0, session="New York", zone_kind="RANGE",
             direction_source="range", entry_hour=16)
        for _ in range(MIN_EDGE_SAMPLE)
    ]
    text = _stats(rows, tmp_path)
    assert text.count("<pre>") == text.count("</pre>")
    assert text.count("<b>") == text.count("</b>")
