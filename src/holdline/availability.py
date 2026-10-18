"""What can actually be promised to a caller right now.

    sellable = on_hand
             - active holds
             - pending-approval holds
             - safety buffer

Two rules are enforced HERE, in code, and nowhere else:

1. The staleness gate. If the POS sync is older than STALE_THRESHOLD_SECONDS
   this returns status "unknown" and never a number. A prompt instruction would
   be a request; this is a guarantee.

2. Pending-approval holds count. A large order waiting for a manager has not
   been granted, but the stock behind it must not be promised to the next
   caller, or two people are promised the same cakes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from holdline import clock, config
from holdline.inventory import get_stock

LIVE_HOLD_STATUSES = ("active", "pending_approval")


@dataclass(frozen=True)
class AvailabilityResult:
    """Exactly one of two shapes: known with a quantity, or unknown with a reason."""

    status: Literal["known", "unknown"]
    item_id: str
    item_name: str
    quantity: int | None = None
    as_of: datetime | None = None
    reason: str | None = None
    age_seconds: int | None = None

    def to_dict(self) -> dict:
        """Tool-result shape. `quantity` is absent, not null, when unknown so the
        guardrail has nothing numeric to find."""
        if self.status == "known":
            return {
                "status": "known",
                "item_id": self.item_id,
                "item_name": self.item_name,
                "quantity": self.quantity,
                "as_of": clock.iso(self.as_of),
            }
        return {
            "status": "unknown",
            "item_id": self.item_id,
            "item_name": self.item_name,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
        }


def live_held_quantity(conn: sqlite3.Connection, location_id: str, item_id: str) -> int:
    """Units currently reserved by active or pending-approval holds."""
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS held FROM holds"
        " WHERE location_id = ? AND item_id = ? AND status IN (?, ?)",
        (location_id, item_id, *LIVE_HOLD_STATUSES),
    ).fetchone()
    return int(row["held"])


def sellable(conn: sqlite3.Connection, location_id: str, item_id: str) -> AvailabilityResult:
    """Compute what may be promised. Takes a connection so holds.create_hold can
    call it INSIDE its write transaction (see holds.py for why that matters)."""
    item = conn.execute("SELECT name, safety_buffer FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        from holdline.exceptions import UnknownItemError

        raise UnknownItemError(item_id)

    on_hand, synced_at = get_stock(conn, location_id, item_id)
    age = int((clock.now() - synced_at).total_seconds())
    if age > config.STALE_THRESHOLD_SECONDS:
        return AvailabilityResult(
            status="unknown",
            item_id=item_id,
            item_name=item["name"],
            reason="stale_data",
            age_seconds=age,
        )

    held = live_held_quantity(conn, location_id, item_id)
    quantity = max(0, on_hand - held - int(item["safety_buffer"]))
    return AvailabilityResult(
        status="known",
        item_id=item_id,
        item_name=item["name"],
        quantity=quantity,
        as_of=synced_at,
    )
