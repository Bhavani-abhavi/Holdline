"""Helpers for arranging inventory and ledger state in tests.

These write to the database directly rather than going through `inventory` and
`holds`. That is deliberate: a test that arranges its precondition using the same
code it is about to test can pass while both are wrong. Arranging with raw SQL
means the arrangement is independent of the thing under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from holdline import config, db
from holdline.timeutils import to_iso, utc_now

#: The location every test uses.
LOCATION_ID = config.DEFAULT_LOCATION_ID

#: A slow-moving, high-value item with a safety buffer of 1.
CAKE = "cake_chocolate_9"

#: A fast-moving item with a safety buffer of 2.
CROISSANT = "pastry_butter_croissant"

#: An item flagged gluten-free that also declares wheat cross-contact.
BROWNIE = "pastry_flourless_brownie"


def set_stock(
    item_id: str,
    on_hand: int,
    *,
    safety_buffer: int | None = None,
    synced_at: datetime | None = None,
    location_id: str = LOCATION_ID,
) -> None:
    """Force an item's stock level, buffer and sync timestamp.

    Args:
        item_id: Catalogue item id.
        on_hand: Physical units on the shelf.
        safety_buffer: Override the item's buffer. Left alone when omitted.
        synced_at: Override the sync timestamp, to age the data. Defaults to now.
        location_id: Which location.
    """
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE stock SET on_hand = ?, last_synced_at = ?"
            " WHERE location_id = ? AND item_id = ?",
            (on_hand, to_iso(synced_at or utc_now()), location_id, item_id),
        )
        if safety_buffer is not None:
            conn.execute(
                "UPDATE items SET safety_buffer = ? WHERE id = ?",
                (safety_buffer, item_id),
            )


def age_sync(item_id: str, seconds: int, *, location_id: str = LOCATION_ID) -> None:
    """Backdate one item's sync timestamp by `seconds`."""
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE stock SET last_synced_at = ? WHERE location_id = ? AND item_id = ?",
            (to_iso(utc_now() - timedelta(seconds=seconds)), location_id, item_id),
        )


def insert_hold(
    item_id: str,
    quantity: int,
    *,
    status: str = "active",
    expires_in_minutes: int = 60,
    pickup_in_minutes: int = 15,
    dietary_note: str | None = None,
    customer_name: str | None = "Existing Customer",
    location_id: str = LOCATION_ID,
) -> int:
    """Insert a hold straight into the ledger and return its id.

    Bypasses `holds.create_hold` on purpose, so that tests of availability and
    expiry can arrange arbitrary ledger states -- including ones `create_hold`
    would refuse to produce.
    """
    now = utc_now()
    with db.transaction(immediate=True) as conn:
        cursor = conn.execute(
            """
            INSERT INTO holds (
                location_id, item_id, quantity, customer_name, dietary_note,
                status, created_at, pickup_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location_id,
                item_id,
                quantity,
                customer_name,
                dietary_note,
                status,
                to_iso(now),
                to_iso(now + timedelta(minutes=pickup_in_minutes)),
                to_iso(now + timedelta(minutes=expires_in_minutes)),
            ),
        )
        return int(cursor.lastrowid or 0)


def hold_status(hold_id: int) -> str | None:
    """Return one hold's current status, or None if it does not exist."""
    row = db.query_one("SELECT status FROM holds WHERE id = ?", (hold_id,))
    return None if row is None else row["status"]


def count_holds(*, status: str | None = None, item_id: str | None = None) -> int:
    """Count holds, optionally filtered by status and item."""
    sql = "SELECT COUNT(*) AS n FROM holds WHERE 1 = 1"
    params: list[object] = []
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    if item_id is not None:
        sql += " AND item_id = ?"
        params.append(item_id)
    row = db.query_one(sql, tuple(params))
    return 0 if row is None else int(row["n"])


def count_live_holds(item_id: str | None = None) -> int:
    """Count holds that reserve stock, whether active or awaiting approval.

    "Live" rather than "active" because both statuses take units off the line, and
    a test asserting that exactly one caller won needs to count both -- otherwise a
    hold that correctly landed in the approval queue would look like no hold.
    """
    sql = "SELECT COUNT(*) AS n FROM holds WHERE status IN ('active', 'pending_approval')"
    params: tuple[object, ...] = ()
    if item_id is not None:
        sql += " AND item_id = ?"
        params = (item_id,)
    row = db.query_one(sql, params)
    return 0 if row is None else int(row["n"])


def on_hand(item_id: str, *, location_id: str = LOCATION_ID) -> int:
    """Return physical stock for one item."""
    row = db.query_one(
        "SELECT on_hand FROM stock WHERE location_id = ? AND item_id = ?",
        (location_id, item_id),
    )
    assert row is not None, f"no stock row for {item_id}"
    return int(row["on_hand"])


def alerts_of_kind(kind: str) -> list[dict[str, object]]:
    """Return every staff alert of one kind, oldest first."""
    rows = db.query(
        "SELECT id, kind, urgency, summary FROM staff_alerts WHERE kind = ? ORDER BY id",
        (kind,),
    )
    return [dict(row) for row in rows]


def events_of_kind(kind: str) -> list[dict[str, object]]:
    """Return every event of one kind, oldest first."""
    rows = db.query("SELECT id, kind, detail FROM events WHERE kind = ? ORDER BY id", (kind,))
    return [dict(row) for row in rows]


def clear_ledger() -> None:
    """Delete holds, events and alerts, leaving the catalogue in place.

    Used between repetitions of the concurrency test so each run starts from a
    known ledger without re-seeding the catalogue.
    """
    with db.transaction(immediate=True) as conn:
        conn.execute("DELETE FROM holds")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM staff_alerts")
