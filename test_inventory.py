"""The mocked POS: stock movements, sync timestamps, and the demo controls."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from holdline import config, db, inventory
from holdline.exceptions import InsufficientStockError, ItemNotFoundError
from holdline.timeutils import age_seconds

from helpers import (
    CAKE,
    CROISSANT,
    LOCATION_ID,
    age_sync,
    alerts_of_kind,
    events_of_kind,
    insert_hold,
    on_hand,
    set_stock,
)


def test_a_stock_reading_carries_the_instant_it_was_true_at(seeded_db: Path) -> None:
    """The whole reason the POS is modelled this way.

    Toast and Square both report a quantity plus an "as of", because their own data
    is a cache of what the register last said. A reading without its timestamp is
    exactly the representation that lets a system promise stock it cannot vouch for.
    """
    quantity, as_of = inventory.get_stock(LOCATION_ID, CAKE)

    assert quantity == 12
    assert age_seconds(as_of) < 60


def test_reading_an_item_the_bakery_does_not_carry_raises(seeded_db: Path) -> None:
    """Distinct from a quantity of zero: "we don't make that" is a different answer."""
    with pytest.raises(ItemNotFoundError):
        inventory.get_stock(LOCATION_ID, "cake_wedding_five_tier")


def test_a_sale_reduces_stock_and_refreshes_the_sync(seeded_db: Path) -> None:
    """A sale is also a sync: the register just told us what it knows.

    This mirrors the real APIs, whose timestamps advance when the register reports,
    and it means an active counter keeps the phone line able to quote.
    """
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS * 2)

    inventory.record_sale(LOCATION_ID, CAKE, 2)

    quantity, as_of = inventory.get_stock(LOCATION_ID, CAKE)
    assert quantity == 10
    assert age_seconds(as_of) < 60


def test_a_sale_is_recorded_as_an_event(seeded_db: Path) -> None:
    """Phase 8's demand history is built from these rows."""
    inventory.record_sale(LOCATION_ID, CAKE, 3)

    sales = events_of_kind("sale")
    assert len(sales) == 1
    assert sales[0]["detail"] is not None


def test_the_counter_cannot_sell_what_is_not_on_the_shelf(seeded_db: Path) -> None:
    """Refusing loudly beats writing an impossible inventory level.

    A negative on-hand figure would poison every availability calculation that read
    it, and quietly clamping to zero would hide whatever produced the bad call.
    """
    set_stock(CAKE, on_hand=2)

    with pytest.raises(InsufficientStockError):
        inventory.record_sale(LOCATION_ID, CAKE, 5)

    assert on_hand(CAKE) == 2


@pytest.mark.parametrize("quantity", [0, -3])
def test_a_nonsense_sale_quantity_is_rejected(seeded_db: Path, quantity: int) -> None:
    """Zero and negative sales are caller bugs, not voids to be modelled."""
    with pytest.raises(ValueError, match="must be positive"):
        inventory.record_sale(LOCATION_ID, CAKE, quantity)


def test_selling_below_what_holds_promised_raises_an_urgent_alert(seeded_db: Path) -> None:
    """Degraded but working, and staff hear about it immediately.

    This state is reachable and is not a bug: the counter sells to walk-ins who never
    called, and the safety buffer only reduces the odds. The alternative to alerting
    is a customer arriving to collect a cake that was sold to someone standing in the
    shop.
    """
    set_stock(CAKE, on_hand=5, safety_buffer=1)
    insert_hold(CAKE, 4, status="active")

    inventory.record_sale(LOCATION_ID, CAKE, 3)  # leaves 2 on hand against 4 promised

    alerts = alerts_of_kind("stock_below_commitments")
    assert len(alerts) == 1
    assert alerts[0]["urgency"] == "urgent"
    assert len(events_of_kind("stock_below_commitments")) == 1


def test_an_oversell_alert_does_not_cancel_anybodys_hold(seeded_db: Path) -> None:
    """The system reports the conflict and lets staff decide what to do about it.

    Automatically cancelling the newest hold would be a system deciding which
    customer to disappoint, which is a judgment it has no standing to make.
    """
    set_stock(CAKE, on_hand=5, safety_buffer=1)
    hold_id = insert_hold(CAKE, 4, status="active")

    inventory.record_sale(LOCATION_ID, CAKE, 3)

    row = db.query_one("SELECT status FROM holds WHERE id = ?", (hold_id,))
    assert row is not None
    assert row["status"] == "active"


def test_a_normal_sale_raises_no_alert(seeded_db: Path) -> None:
    """The oversell alert must be rare, or staff will learn to ignore it."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    insert_hold(CAKE, 2, status="active")

    inventory.record_sale(LOCATION_ID, CAKE, 1)

    assert alerts_of_kind("stock_below_commitments") == []


def test_a_delivery_raises_stock_and_is_written_down(seeded_db: Path) -> None:
    """Deliveries are their own record; Phase 8 compares them against demand."""
    set_stock(CAKE, on_hand=4)

    new_total = inventory.receive_delivery(LOCATION_ID, CAKE, 6, note="afternoon bake")

    assert new_total == 10
    assert on_hand(CAKE) == 10
    rows = db.query("SELECT quantity, note FROM deliveries")
    assert len(rows) == 1
    assert rows[0]["quantity"] == 6
    assert rows[0]["note"] == "afternoon bake"
    assert len(events_of_kind("delivery")) == 1


def test_a_delivery_refreshes_the_sync_so_quoting_resumes(seeded_db: Path) -> None:
    """Receiving stock is a register event too, so it makes the data current again."""
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS * 2)

    inventory.receive_delivery(LOCATION_ID, CAKE, 3)

    _, as_of = inventory.get_stock(LOCATION_ID, CAKE)
    assert age_seconds(as_of) < 60


@pytest.mark.parametrize("quantity", [0, -2])
def test_a_nonsense_delivery_quantity_is_rejected(seeded_db: Path, quantity: int) -> None:
    """A zero-unit delivery is a mistake somewhere upstream, not a no-op to absorb."""
    with pytest.raises(ValueError, match="must be positive"):
        inventory.receive_delivery(LOCATION_ID, CAKE, quantity)


def test_simulated_sales_are_reproducible_with_a_seeded_generator(seeded_db: Path) -> None:
    """A demo control still needs to be testable, so the generator is injectable."""
    sold = inventory.simulate_sales(60, rng=random.Random(1234))

    assert sold > 0
    assert len(events_of_kind("sale")) > 0


def test_simulated_sales_follow_the_per_category_demand_profile(seeded_db: Path) -> None:
    """Pastries move constantly, whole cakes a few times a day, drinks not at all.

    Uniform demand would make the simulated rush look nothing like a bakery, and the
    Phase 8 forecast would then be fitting a shape no real shop has.
    """
    before_croissant = on_hand(CROISSANT)
    before_drink = on_hand("drink_cold_brew")

    inventory.simulate_sales(120, rng=random.Random(7))

    assert on_hand(CROISSANT) < before_croissant
    # Drinks are made to order, so the simulation leaves them alone entirely.
    assert on_hand("drink_cold_brew") == before_drink
    assert config.SIMULATED_SALES_PER_HOUR["drinks"] == 0


def test_a_simulated_rush_never_drives_stock_negative(seeded_db: Path) -> None:
    """The demo must survive being run repeatedly on a nearly empty shop."""
    set_stock(CROISSANT, on_hand=1)

    for _ in range(5):
        inventory.simulate_sales(60, rng=random.Random(99))

    assert on_hand(CROISSANT) >= 0


@pytest.mark.parametrize("minutes", [0, -10])
def test_simulating_a_nonsense_time_span_is_rejected(seeded_db: Path, minutes: int) -> None:
    """Demo controls get wired to HTTP endpoints, so their inputs are validated too."""
    with pytest.raises(ValueError, match="must be positive"):
        inventory.simulate_sales(minutes)


def test_breaking_the_sync_ages_every_item_well_past_the_threshold(seeded_db: Path) -> None:
    """Unambiguously stale, not sitting on the boundary.

    A demo that landed exactly on the threshold could go either way depending on how
    long the presenter talks, which is the worst possible property for a live demo.
    """
    affected = inventory.force_stale(LOCATION_ID)

    assert affected == 12
    _, as_of = inventory.get_stock(LOCATION_ID, CAKE)
    assert age_seconds(as_of) > config.STALE_THRESHOLD_SECONDS * 2


def test_breaking_the_sync_for_one_item_leaves_the_others_current(seeded_db: Path) -> None:
    """Targeted breakage, so a demo can show one item going dark beside eleven live ones."""
    inventory.force_stale(LOCATION_ID, CAKE)

    _, cake_as_of = inventory.get_stock(LOCATION_ID, CAKE)
    _, croissant_as_of = inventory.get_stock(LOCATION_ID, CROISSANT)

    assert age_seconds(cake_as_of) > config.STALE_THRESHOLD_SECONDS
    assert age_seconds(croissant_as_of) < 60


def test_restoring_the_sync_makes_every_item_current_again(seeded_db: Path) -> None:
    """Not in the specification, and necessary: breaking things needs an undo."""
    inventory.force_stale(LOCATION_ID)

    affected = inventory.restore_sync(LOCATION_ID)

    assert affected == 12
    _, as_of = inventory.get_stock(LOCATION_ID, CAKE)
    assert age_seconds(as_of) < 60
