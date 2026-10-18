"""sellable() arithmetic: buffer, live holds, never negative."""

from __future__ import annotations

from datetime import timedelta

from holdline import clock, inventory
from holdline.availability import sellable
from holdline.holds import create_hold


def test_sellable_subtracts_safety_buffer(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 11)  # buffer is 1
    with db.connection() as conn:
        result = sellable(conn, location_id, "cake_choc_9")
    assert result.status == "known"
    assert result.quantity == 10


def test_active_and_pending_holds_both_decrement(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 11)
    pickup = clock.now() + timedelta(hours=1)
    # 1 unit -> active; 3 units -> pending_approval (above MAX_AUTO_APPROVE_QTY)
    assert (
        create_hold(
            db,
            location_id=location_id,
            item_id="cake_choc_9",
            quantity=1,
            pickup_at=pickup,
            call_id="a",
        ).status
        == "active"
    )
    assert (
        create_hold(
            db,
            location_id=location_id,
            item_id="cake_choc_9",
            quantity=3,
            pickup_at=pickup,
            call_id="b",
        ).status
        == "pending_approval"
    )
    with db.connection() as conn:
        result = sellable(conn, location_id, "cake_choc_9")
    assert result.quantity == 11 - 1 - 1 - 3


def test_sellable_never_negative(db, location_id):
    inventory.set_stock(db, location_id, "cake_choc_9", 0)
    with db.connection() as conn:
        result = sellable(conn, location_id, "cake_choc_9")
    assert result.quantity == 0


def test_delivery_raises_sellable(db, location_id):
    inventory.set_stock(db, location_id, "cake_carrot", 2)
    inventory.receive_delivery(db, location_id, "cake_carrot", 10, note="Monday van")
    with db.connection() as conn:
        result = sellable(conn, location_id, "cake_carrot")
    assert result.quantity == 2 + 10 - 1


def test_sale_lowers_sellable_and_never_below_zero(db, location_id):
    inventory.set_stock(db, location_id, "cake_carrot", 2)
    inventory.record_sale(db, location_id, "cake_carrot", 5)
    with db.connection() as conn:
        on_hand, _ = inventory.get_stock(conn, location_id, "cake_carrot")
        result = sellable(conn, location_id, "cake_carrot")
    assert on_hand == 0
    assert result.quantity == 0
