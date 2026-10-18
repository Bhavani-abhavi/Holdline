"""Hold lifecycle, approval policy and idempotency."""

from __future__ import annotations

from datetime import timedelta

from holdline import clock, inventory
from holdline.availability import sellable
from holdline.holds import (
    approve_hold,
    cancel_hold,
    collect_hold,
    create_hold,
    decline_hold,
    expire_due_holds,
    get_active_holds,
)
from holdline.notifications import open_alerts


def _hold(db, location_id, **overrides):
    params = dict(
        location_id=location_id,
        item_id="cake_choc_9",
        quantity=1,
        pickup_at=clock.now() + timedelta(hours=1),
        call_id="call-1",
        channel="browser",
    )
    params.update(overrides)
    return create_hold(db, **params)


def test_small_hold_is_active_and_alerts_staff(db, location_id):
    result = _hold(db, location_id)
    assert result.ok and result.status == "active" and result.requires_approval is False
    with db.connection() as conn:
        alerts = open_alerts(conn, location_id)
    assert [a["kind"] for a in alerts] == ["new_hold"]


def test_large_hold_needs_approval_and_approve_activates(db, location_id):
    result = _hold(db, location_id, quantity=3)
    assert result.status == "pending_approval"
    assert "quantity_above_auto_limit" in result.approval_reasons
    assert approve_hold(db, result.hold_id) is True
    with db.connection() as conn:
        assert get_active_holds(conn, location_id)[0]["status"] == "active"
    assert approve_hold(db, result.hold_id) is False  # already active


def test_far_pickup_needs_approval(db, location_id):
    result = _hold(db, location_id, pickup_at=clock.now() + timedelta(hours=6))
    assert "pickup_too_far_ahead" in result.approval_reasons


def test_item_at_buffer_needs_approval(db, location_id):
    # cold brew has buffer 0; set on_hand to 0 -> at buffer but sellable 0 -> refused.
    # Use a buffer-1 item at on_hand 1: sellable 0 -> refused. The rule only bites for
    # buffer-0 items... so demonstrate the reason list directly via needs_approval.
    from holdline.holds import needs_approval

    inventory.set_stock(db, location_id, "drink_cold_brew", 0)
    with db.connection() as conn:
        reasons = needs_approval(
            conn,
            location_id,
            "drink_cold_brew",
            1,
            clock.now() + timedelta(hours=1),
            None,
            clock.now(),
        )
    assert "item_at_or_below_buffer" in reasons


def test_decline_releases_stock(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 5)
    result = _hold(db, location_id, quantity=3)
    with db.connection() as conn:
        assert sellable(conn, location_id, "cake_choc_9").quantity == 1
    assert decline_hold(db, result.hold_id)
    with db.connection() as conn:
        assert sellable(conn, location_id, "cake_choc_9").quantity == 4


def test_cancel_and_collect(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 5)
    a = _hold(db, location_id, call_id="a")
    b = _hold(db, location_id, call_id="b")
    assert cancel_hold(db, a.hold_id)
    assert collect_hold(db, b.hold_id)
    with db.connection() as conn:
        on_hand, _ = inventory.get_stock(conn, location_id, "cake_choc_9")
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
        alerts = open_alerts(conn, location_id)
    assert on_hand == 4
    assert "hold_collected" in kinds
    assert any(a["kind"] == "review_request" for a in alerts)  # unconditional, every collection


def test_insufficient_and_invalid(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 2)
    assert _hold(db, location_id, quantity=5).reason == "insufficient"
    assert _hold(db, location_id, quantity=0).reason == "invalid_quantity"
    assert (
        _hold(db, location_id, pickup_at=clock.now() - timedelta(hours=1)).reason
        == "pickup_in_past"
    )


def test_duplicate_create_hold_is_replayed_not_doubled(db, location_id):
    pickup = clock.now() + timedelta(minutes=20)
    first = _hold(db, location_id, pickup_at=pickup)
    # Same caller, same request, microseconds later: must be the same hold.
    second = _hold(db, location_id, pickup_at=pickup + timedelta(microseconds=350))
    assert second.ok and second.replayed and second.hold_id == first.hold_id
    with db.connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0]
    assert n == 1


def test_different_callers_are_not_deduped(db, location_id):
    pickup = clock.now() + timedelta(minutes=20)
    a = _hold(db, location_id, call_id="a", pickup_at=pickup)
    b = _hold(db, location_id, call_id="b", pickup_at=pickup)
    assert a.hold_id != b.hold_id and not b.replayed


def test_staff_hold_without_call_id_has_no_key(db, location_id):
    a = _hold(db, location_id, call_id=None)
    b = _hold(db, location_id, call_id=None)
    assert a.hold_id != b.hold_id


def test_expiry_releases_stock_and_is_idempotent(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 3)
    result = _hold(db, location_id, pickup_at=clock.now() + timedelta(minutes=10))
    assert expire_due_holds(db) == 0
    # Jump forward past pickup + 45 minute grace.
    clock.freeze(clock.now() + timedelta(minutes=60))
    assert expire_due_holds(db) == 1
    assert expire_due_holds(db) == 0
    # An hour has passed, so the POS sync is (correctly) stale; refresh it to read a number.
    inventory.resync(db, location_id)
    with db.connection() as conn:
        assert sellable(conn, location_id, "cake_choc_9").quantity == 2
        hold = conn.execute("SELECT status FROM holds WHERE id = ?", (result.hold_id,)).fetchone()
    assert hold["status"] == "expired"
