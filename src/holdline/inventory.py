"""The mocked point-of-sale system.

This module stands in for a real POS API. The interface deliberately mirrors
the shape of Toast's Stock API and Square's Inventory API: a status, a quantity
and a sync timestamp per item. That shape is why swapping in a real POS would
be a single adapter that fills the `stock` table, with nothing above this
module changing.

Nothing here knows about holds. Holds live in holds.py; the combination of
stock minus holds is computed in availability.py.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta

from holdline import clock, events
from holdline.db import Database
from holdline.exceptions import UnknownItemError
from holdline.logging_setup import get_logger

log = get_logger(__name__)


def get_stock(conn: sqlite3.Connection, location_id: str, item_id: str) -> tuple[int, datetime]:
    """Return (on_hand, last_synced_at) for one item at one location.

    Raises:
        UnknownItemError: the item is not stocked at that location.
    """
    row = conn.execute(
        "SELECT on_hand, last_synced_at FROM stock WHERE location_id = ? AND item_id = ?",
        (location_id, item_id),
    ).fetchone()
    if row is None:
        raise UnknownItemError(f"{item_id!r} is not stocked at {location_id!r}")
    return int(row["on_hand"]), clock.parse(row["last_synced_at"])


def record_sale(
    db: Database, location_id: str, item_id: str, qty: int, *, channel: str = "pos"
) -> None:
    """Decrement stock for a completed sale and refresh the sync timestamp.

    Stock never goes below zero: a walk-in sale of the last unit while a hold is
    active is a real-world conflict the staff page surfaces, not something the
    POS mock should hide by going negative.
    """
    with db.transaction(immediate=True) as conn:
        on_hand, _ = get_stock(conn, location_id, item_id)
        new_qty = max(0, on_hand - qty)
        conn.execute(
            "UPDATE stock SET on_hand = ?, last_synced_at = ?"
            " WHERE location_id = ? AND item_id = ?",
            (new_qty, clock.iso(), location_id, item_id),
        )
        events.record(
            conn,
            "sale",
            {"item_id": item_id, "quantity": qty, "on_hand_after": new_qty},
            location_id=location_id,
            channel=channel,
        )


def receive_delivery(
    db: Database, location_id: str, item_id: str, qty: int, note: str | None = None
) -> int:
    """Increment stock for a delivery, record it, and return the delivery id.

    This is the restocking path: the bakery receives 50 cakes on Monday morning.
    """
    if qty <= 0:
        raise ValueError("delivery quantity must be positive")
    with db.transaction(immediate=True) as conn:
        on_hand, _ = get_stock(conn, location_id, item_id)
        now = clock.iso()
        conn.execute(
            "UPDATE stock SET on_hand = ?, last_synced_at = ?"
            " WHERE location_id = ? AND item_id = ?",
            (on_hand + qty, now, location_id, item_id),
        )
        cur = conn.execute(
            "INSERT INTO deliveries (location_id, item_id, quantity, received_at, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (location_id, item_id, qty, now, note),
        )
        events.record(
            conn,
            "delivery",
            {"item_id": item_id, "quantity": qty, "on_hand_after": on_hand + qty, "note": note},
            location_id=location_id,
            channel="staff",
        )
        return int(cur.lastrowid or 0)


def simulate_sales(db: Database, location_id: str, minutes: int, *, seed: int | None = None) -> int:
    """Demo control: generate random sales as if `minutes` of trading had passed.

    Returns the number of units sold. Roughly one sale every two minutes.
    """
    rng = random.Random(seed)
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT item_id, on_hand FROM stock WHERE location_id = ? AND on_hand > 0",
            (location_id,),
        ).fetchall()
    if not rows:
        return 0
    sold = 0
    for _ in range(max(1, minutes // 2)):
        row = rng.choice(rows)
        record_sale(db, location_id, row["item_id"], 1)
        sold += 1
    log.info("simulated_sales", minutes=minutes, units=sold)
    return sold


def force_stale(db: Database, location_id: str, item_id: str | None = None) -> None:
    """Demo control: push the sync timestamp far into the past.

    With `item_id` None every item at the location goes stale, which is what
    "Break the POS sync" on the customer page does.
    """
    stale_at = clock.iso(clock.now() - timedelta(hours=6))
    with db.transaction(immediate=True) as conn:
        if item_id is None:
            conn.execute(
                "UPDATE stock SET last_synced_at = ? WHERE location_id = ?", (stale_at, location_id)
            )
        else:
            conn.execute(
                "UPDATE stock SET last_synced_at = ? WHERE location_id = ? AND item_id = ?",
                (stale_at, location_id, item_id),
            )
        events.record(
            conn, "pos_sync_broken", {"item_id": item_id}, location_id=location_id, channel="pos"
        )


def resync(db: Database, location_id: str) -> None:
    """Demo control: mark every item freshly synced (the opposite of force_stale)."""
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE stock SET last_synced_at = ? WHERE location_id = ?", (clock.iso(), location_id)
        )
        events.record(conn, "pos_sync_restored", {}, location_id=location_id, channel="pos")


def set_stock(db: Database, location_id: str, item_id: str, on_hand: int) -> None:
    """Test and eval helper: set an absolute stock level with a fresh sync time."""
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE stock SET on_hand = ?, last_synced_at = ?"
            " WHERE location_id = ? AND item_id = ?",
            (on_hand, clock.iso(), location_id, item_id),
        )
