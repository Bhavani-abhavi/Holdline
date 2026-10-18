"""The staleness gate: stale data returns unknown, leaks no number, refuses holds."""

from __future__ import annotations

from datetime import timedelta

from holdline import clock, config, inventory
from holdline.availability import sellable
from holdline.holds import create_hold


def test_stale_returns_unknown_with_no_quantity(db, location_id):
    inventory.force_stale(db, location_id, "cake_choc_9")
    with db.connection() as conn:
        result = sellable(conn, location_id, "cake_choc_9")
    assert result.status == "unknown"
    assert result.reason == "stale_data"
    assert result.quantity is None
    assert "quantity" not in result.to_dict()
    assert result.age_seconds > config.STALE_THRESHOLD_SECONDS


def test_threshold_boundary_is_inclusive_of_fresh(db, location_id):
    # Exactly at the threshold is still fresh; one second past is stale.
    with db.transaction(immediate=True) as conn:
        at = clock.iso(clock.now() - timedelta(seconds=config.STALE_THRESHOLD_SECONDS))
        conn.execute("UPDATE stock SET last_synced_at = ? WHERE item_id = 'cake_choc_9'", (at,))
    with db.connection() as conn:
        assert sellable(conn, location_id, "cake_choc_9").status == "known"
    with db.transaction(immediate=True) as conn:
        at = clock.iso(clock.now() - timedelta(seconds=config.STALE_THRESHOLD_SECONDS + 1))
        conn.execute("UPDATE stock SET last_synced_at = ? WHERE item_id = 'cake_choc_9'", (at,))
    with db.connection() as conn:
        assert sellable(conn, location_id, "cake_choc_9").status == "unknown"


def test_hold_refused_under_stale_data(db, location_id):
    inventory.force_stale(db, location_id)
    result = create_hold(
        db,
        location_id=location_id,
        item_id="cake_choc_9",
        quantity=1,
        pickup_at=clock.now() + timedelta(hours=1),
        call_id="c1",
    )
    assert result.ok is False
    assert result.reason == "availability_unknown"


def test_resync_restores_numbers(db, location_id):
    inventory.force_stale(db, location_id)
    inventory.resync(db, location_id)
    with db.connection() as conn:
        assert sellable(conn, location_id, "cake_choc_9").status == "known"
