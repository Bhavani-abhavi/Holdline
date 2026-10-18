"""The hold ledger. Concurrency and idempotency live here.

create_hold is the one place stock is promised to a caller. Three guarantees:

ATOMIC. The availability check happens INSIDE the write transaction, not before
it. Checking first and then writing leaves a window where another caller takes
the last unit between the two operations: a time-of-check-to-time-of-use race.
BEGIN IMMEDIATE takes SQLite's write lock before the read, so for every caller
the check and the insert are one indivisible step. Ten threads racing for one
cake produce exactly one active hold; tests/test_concurrency.py proves it 20x.

IDEMPOTENT. The model occasionally calls create_hold twice in one turn. The key
is derived from (call_id, location, item, quantity, pickup minute) and looked up
inside the same transaction, and a partial unique index backs it up. Pickup is
truncated to the MINUTE: two resolutions of "in twenty minutes" within one turn
land microseconds apart, so a key built from the raw instant would dedupe
nothing.

POLICY IN CODE. create_hold itself decides pending_approval versus active and
refuses impossible requests. The model is never asked to apply policy.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from holdline import clock, config, events
from holdline.availability import sellable
from holdline.db import Database
from holdline.dietary import assess_request
from holdline.inventory import get_stock
from holdline.logging_setup import get_logger, phone_fingerprint
from holdline.notifications import fan_out

log = get_logger(__name__)

HoldStatus = Literal["pending_approval", "active", "collected", "expired", "cancelled", "declined"]


@dataclass(frozen=True)
class HoldResult:
    """Outcome of create_hold. `ok` False carries a machine-readable reason."""

    ok: bool
    hold_id: int | None = None
    status: str | None = None
    expires_at: str | None = None
    reason: str | None = None
    message: str | None = None
    replayed: bool = False
    requires_approval: bool = False
    approval_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            data.update(
                hold_id=self.hold_id,
                status=self.status,
                expires_at=self.expires_at,
                requires_approval=self.requires_approval,
                approval_reasons=list(self.approval_reasons),
            )
            if self.replayed:
                data["note"] = "duplicate request; returning the existing hold"
        else:
            data.update(reason=self.reason, message=self.message)
        return data


def idempotency_key(
    call_id: str | None, location_id: str, item_id: str, quantity: int, pickup_at: datetime
) -> str | None:
    """Stable key for one caller asking for one thing at one pickup minute.

    Without a call_id (a staff-created hold, say) there is nothing to dedupe
    against, so no key is produced and the unique index does not apply.
    """
    if not call_id:
        return None
    minute = pickup_at.replace(second=0, microsecond=0).isoformat()
    raw = "|".join([call_id, location_id, item_id, str(quantity), minute])
    return hashlib.sha256(raw.encode()).hexdigest()


def needs_approval(
    conn: sqlite3.Connection,
    location_id: str,
    item_id: str,
    quantity: int,
    pickup_at: datetime,
    dietary_note: str | None,
    now: datetime,
) -> list[str]:
    """Return the list of reasons a human must decide; empty means auto-approve."""
    reasons: list[str] = []
    if quantity > config.MAX_AUTO_APPROVE_QTY:
        reasons.append("quantity_above_auto_limit")
    if pickup_at - now > timedelta(hours=config.MAX_HOLD_HOURS_AHEAD):
        reasons.append("pickup_too_far_ahead")
    on_hand, _ = get_stock(conn, location_id, item_id)
    buffer = conn.execute("SELECT safety_buffer FROM items WHERE id = ?", (item_id,)).fetchone()
    if on_hand <= int(buffer["safety_buffer"]):
        reasons.append("item_at_or_below_buffer")
    assessment = assess_request(dietary_note)
    if assessment.severity == "allergy" and config.ALLERGY_ALWAYS_ESCALATE:
        reasons.append("allergy_declared")
    elif dietary_note and dietary_note.strip():
        reasons.append("dietary_note_present")
    return reasons


def create_hold(
    db: Database,
    *,
    location_id: str,
    item_id: str,
    quantity: int,
    pickup_at: datetime,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    dietary_note: str | None = None,
    call_id: str | None = None,
    channel: str | None = None,
) -> HoldResult:
    """Atomically reserve stock. See the module docstring for the guarantees."""
    if quantity <= 0:
        return HoldResult(ok=False, reason="invalid_quantity", message="Quantity must be positive.")
    now = clock.now()
    if pickup_at < now - timedelta(minutes=1):
        return HoldResult(ok=False, reason="pickup_in_past", message="Pickup time is in the past.")

    key = idempotency_key(call_id, location_id, item_id, quantity, pickup_at)

    with db.transaction(immediate=True) as conn:
        # 1. Replay check, inside the lock so two racing duplicates cannot both insert.
        if key is not None:
            existing = conn.execute(
                "SELECT id, status, expires_at FROM holds WHERE idempotency_key = ?"
                " AND status IN ('pending_approval', 'active')",
                (key,),
            ).fetchone()
            if existing is not None:
                log.info("hold_replayed", hold_id=existing["id"], call_id=call_id)
                return HoldResult(
                    ok=True,
                    hold_id=int(existing["id"]),
                    status=existing["status"],
                    expires_at=existing["expires_at"],
                    replayed=True,
                    requires_approval=existing["status"] == "pending_approval",
                )

        # 2. Availability, re-read INSIDE the transaction. This is the TOCTOU fix.
        avail = sellable(conn, location_id, item_id)
        if avail.status != "known":
            return HoldResult(
                ok=False,
                reason="availability_unknown",
                message="Inventory data is stale; a person must confirm before anything is held.",
            )
        if (avail.quantity or 0) < quantity:
            return HoldResult(
                ok=False,
                reason="insufficient",
                message=f"Only {avail.quantity} sellable right now.",
            )

        # 3. Policy decides the status. Not the model.
        reasons = needs_approval(conn, location_id, item_id, quantity, pickup_at, dietary_note, now)
        status: HoldStatus = "pending_approval" if reasons else "active"
        expires_at = pickup_at + timedelta(minutes=config.DEFAULT_HOLD_MINUTES)

        cur = conn.execute(
            "INSERT INTO holds (location_id, item_id, quantity, customer_name, customer_phone,"
            " dietary_note, status, call_id, channel, idempotency_key, pickup_at, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                location_id,
                item_id,
                quantity,
                customer_name,
                customer_phone,
                dietary_note,
                status,
                call_id,
                channel,
                key,
                clock.iso(pickup_at),
                clock.iso(now),
                clock.iso(expires_at),
            ),
        )
        hold_id = int(cur.lastrowid or 0)

        # 4. Alert and event commit with the hold.
        payload = {
            "hold_id": hold_id,
            "item_id": item_id,
            "item_name": avail.item_name,
            "quantity": quantity,
            "pickup_at": clock.iso(pickup_at),
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "dietary_note": dietary_note,
            "approval_reasons": reasons,
            "call_id": call_id,
        }
        if "allergy_declared" in reasons:
            fan_out(
                conn,
                location_id,
                "allergy_callback",
                "urgent",
                f"Allergy declared: call back about {avail.item_name} x{quantity}",
                payload,
            )
        elif reasons:
            fan_out(
                conn,
                location_id,
                "approval_needed",
                "action",
                f"Approval needed: {avail.item_name} x{quantity}",
                payload,
            )
        else:
            fan_out(
                conn,
                location_id,
                "new_hold",
                "info",
                f"New hold: {avail.item_name} x{quantity}",
                payload,
            )
        events.record(
            conn,
            "hold_created",
            {
                "hold_id": hold_id,
                "item_id": item_id,
                "quantity": quantity,
                "status": status,
                "approval_reasons": reasons,
                "phone_fp": phone_fingerprint(customer_phone),
            },
            location_id=location_id,
            channel=channel,
            call_id=call_id,
        )
        log.info("hold_created", hold_id=hold_id, item_id=item_id, quantity=quantity, status=status)
        return HoldResult(
            ok=True,
            hold_id=hold_id,
            status=status,
            expires_at=clock.iso(expires_at),
            requires_approval=bool(reasons),
            approval_reasons=tuple(reasons),
        )


def _transition(
    db: Database,
    hold_id: int,
    from_statuses: tuple[str, ...],
    to_status: HoldStatus,
    event_kind: str,
) -> bool:
    """Move a hold between statuses atomically. Returns False if it was not in a from-status."""
    with db.transaction(immediate=True) as conn:
        row = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if row is None or row["status"] not in from_statuses:
            return False
        conn.execute(
            "UPDATE holds SET status = ?, resolved_at = ? WHERE id = ?",
            (to_status, clock.iso(), hold_id),
        )
        events.record(
            conn,
            event_kind,
            {
                "hold_id": hold_id,
                "item_id": row["item_id"],
                "quantity": row["quantity"],
                "from": row["status"],
                "to": to_status,
            },
            location_id=row["location_id"],
            channel=row["channel"],
            call_id=row["call_id"],
        )
        return True


def approve_hold(db: Database, hold_id: int) -> bool:
    """Staff approve: pending_approval -> active."""
    ok = _transition(db, hold_id, ("pending_approval",), "active", "hold_approved")
    return ok


def decline_hold(db: Database, hold_id: int) -> bool:
    """Staff decline: pending_approval -> declined. Stock is released."""
    return _transition(db, hold_id, ("pending_approval",), "declined", "hold_declined")


def cancel_hold(db: Database, hold_id: int) -> bool:
    """Caller cancels: any live hold -> cancelled."""
    return _transition(db, hold_id, ("pending_approval", "active"), "cancelled", "hold_cancelled")


def collect_hold(db: Database, hold_id: int) -> bool:
    """Customer picked up: active -> collected, and the units leave stock.

    The Google-review request lives at THIS call site, unconditionally, for every
    collection. Not in the prompt: sentiment-gated solicitation violates Google's
    review policies, and a prompt is a request while a call site is a guarantee.
    """
    with db.transaction(immediate=True) as conn:
        row = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if row is None or row["status"] != "active":
            return False
        conn.execute(
            "UPDATE holds SET status = 'collected', resolved_at = ? WHERE id = ?",
            (clock.iso(), hold_id),
        )
        conn.execute(
            "UPDATE stock SET on_hand = MAX(0, on_hand - ?), last_synced_at = ?"
            " WHERE location_id = ? AND item_id = ?",
            (row["quantity"], clock.iso(), row["location_id"], row["item_id"]),
        )
        events.record(
            conn,
            "hold_collected",
            {
                "hold_id": hold_id,
                "item_id": row["item_id"],
                "quantity": row["quantity"],
                "review_requested": True,
            },
            location_id=row["location_id"],
            channel=row["channel"],
            call_id=row["call_id"],
        )
        fan_out(
            conn,
            row["location_id"],
            "review_request",
            "info",
            "Hold collected; review request sent to every customer by policy",
            {"hold_id": hold_id, "customer_phone": row["customer_phone"]},
        )
        return True


def expire_due_holds(db: Database) -> int:
    """Release every live hold past expires_at. Idempotent; returns how many expired."""
    now_iso = clock.iso()
    expired = 0
    with db.transaction(immediate=True) as conn:
        rows = conn.execute(
            "SELECT * FROM holds WHERE status IN ('pending_approval', 'active') AND expires_at < ?",
            (now_iso,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE holds SET status = 'expired', resolved_at = ? WHERE id = ?",
                (now_iso, row["id"]),
            )
            events.record(
                conn,
                "hold_expired",
                {"hold_id": row["id"], "item_id": row["item_id"], "quantity": row["quantity"]},
                location_id=row["location_id"],
                channel=row["channel"],
                call_id=row["call_id"],
            )
            fan_out(
                conn,
                row["location_id"],
                "hold_expired",
                "info",
                f"Hold #{row['id']} expired and stock released",
                {"hold_id": row["id"], "item_id": row["item_id"], "quantity": row["quantity"]},
            )
            expired += 1
    if expired:
        log.info("holds_expired", count=expired)
    return expired


def get_active_holds(
    conn: sqlite3.Connection, location_id: str | None = None
) -> list[dict[str, Any]]:
    """Live holds (active and pending_approval), soonest pickup first."""
    sql = (
        "SELECT h.*, i.name AS item_name FROM holds h JOIN items i ON i.id = h.item_id"
        " WHERE h.status IN ('pending_approval', 'active')"
    )
    params: tuple[Any, ...] = ()
    if location_id:
        sql += " AND h.location_id = ?"
        params = (location_id,)
    sql += " ORDER BY h.pickup_at, h.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_hold(conn: sqlite3.Connection, hold_id: int) -> dict[str, Any] | None:
    """One hold by id, or None."""
    row = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
    return dict(row) if row else None
