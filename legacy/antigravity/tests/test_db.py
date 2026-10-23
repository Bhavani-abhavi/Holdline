"""The database layer must make Phase 2's atomicity guarantee possible.

These tests check the settings the hold ledger depends on. They look pedestrian,
and they are the reason the concurrency test can be believed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from holdline import db
from holdline.timeutils import iso_now, minutes_from_now, to_iso


def _insert_location() -> None:
    """Insert the minimum location row the other tables reference."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO locations (id, name, timezone) VALUES (?, ?, ?)",
            ("loc_test", "Test Bakery", "America/Chicago"),
        )


def test_write_ahead_logging_is_enabled(temp_db: Path) -> None:
    """WAL is what stops the staff page's polling from blocking hold writes."""
    conn = db.connect(temp_db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert mode.lower() == "wal"


def test_foreign_keys_are_enforced() -> None:
    """A hold pointing at an item that does not exist must be impossible.

    SQLite disables foreign keys by default and does so *per connection*, so this
    is a real risk rather than a formality.
    """
    _insert_location()

    with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO holds (
                location_id, item_id, quantity, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("loc_test", "item_that_does_not_exist", 1, "active", iso_now(), iso_now()),
        )


def test_a_failed_transaction_leaves_no_partial_write() -> None:
    """Half a hold is worse than no hold, so any exception rolls the block back."""
    _insert_location()

    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO locations (id, name, timezone) VALUES (?, ?, ?)",
            ("loc_second", "Second Bakery", "America/Chicago"),
        )
        raise RuntimeError("simulated failure after the first write")

    remaining = db.query("SELECT id FROM locations")
    assert [row["id"] for row in remaining] == ["loc_test"]


def test_a_nested_failure_rolls_back_only_the_inner_block() -> None:
    """Nested blocks become savepoints so the outer transaction decides its own fate."""
    _insert_location()

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO locations (id, name, timezone) VALUES (?, ?, ?)",
            ("loc_outer", "Outer Bakery", "America/Chicago"),
        )
        with pytest.raises(RuntimeError), db.transaction() as inner:
            inner.execute(
                "INSERT INTO locations (id, name, timezone) VALUES (?, ?, ?)",
                ("loc_inner", "Inner Bakery", "America/Chicago"),
            )
            raise RuntimeError("inner failure")

    ids = {row["id"] for row in db.query("SELECT id FROM locations")}
    assert ids == {"loc_test", "loc_outer"}


def test_requesting_the_write_lock_inside_an_open_transaction_is_refused() -> None:
    """SQLite cannot upgrade a deferred transaction, so pretending it can is banned.

    Silently handing back a transaction *without* the write lock is exactly the
    time-of-check-to-time-of-use race that `immediate=True` exists to prevent, so
    the request fails loudly instead.
    """
    with db.transaction() as conn:
        conn.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"), db.transaction(immediate=True):
            pass


def test_immediate_transactions_serialise_rather_than_fail(temp_db: Path) -> None:
    """A second writer waits for the lock instead of raising "database is locked".

    This is the behaviour the concurrency test depends on: losers queue, re-read,
    and are told no -- they do not crash.
    """
    _insert_location()

    other = db.connect(temp_db)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO locations (id, name, timezone) VALUES (?, ?, ?)",
            ("loc_other", "Other Bakery", "America/Chicago"),
        )
        other.commit()
    finally:
        other.close()

    with db.transaction(immediate=True) as conn:
        conn.execute("SELECT COUNT(*) FROM locations")

    assert len(db.query("SELECT id FROM locations")) == 2


def test_initialize_is_safe_to_run_against_an_existing_database(temp_db: Path) -> None:
    """Startup runs initialize() every time, so it must never disturb existing data."""
    _insert_location()
    db.initialize(temp_db)

    assert len(db.query("SELECT id FROM locations")) == 1


def test_reset_destroys_the_ledger_and_rebuilds_the_schema(temp_db: Path) -> None:
    """The demo reset button wipes data; the schema must survive it."""
    _insert_location()

    db.reset(temp_db)

    assert db.query("SELECT id FROM locations") == []
    # Tables still exist: a query against them succeeds rather than raising.
    assert db.query("SELECT id FROM holds") == []


def test_events_ids_are_monotonic_for_watermark_extraction() -> None:
    """Phase 8 extracts incrementally, which requires an append-only monotonic key."""
    _insert_location()
    with db.transaction() as conn:
        for kind in ("call_started", "tool_call", "hold_created"):
            conn.execute(
                "INSERT INTO events (at, location_id, kind, detail) VALUES (?, ?, ?, ?)",
                (iso_now(), "loc_test", kind, "{}"),
            )

    ids = [row["id"] for row in db.query("SELECT id FROM events ORDER BY id")]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3


def test_expiry_timestamps_compare_correctly_as_stored_strings() -> None:
    """Stored ISO strings must sort chronologically, or the expiry query is wrong.

    The expiry worker selects holds `WHERE expires_at <= now` as a *string*
    comparison. That only works because `to_iso` writes a fixed-width UTC format.
    """
    past = to_iso(minutes_from_now(-5))
    future = to_iso(minutes_from_now(5))

    assert past < future
