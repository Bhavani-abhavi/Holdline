"""The sellable arithmetic: what may be promised, and what must not be counted."""

from __future__ import annotations

from pathlib import Path

import pytest

from holdline import availability
from holdline.exceptions import ItemNotFoundError

from helpers import CAKE, CROISSANT, LOCATION_ID, insert_hold, set_stock


def test_sellable_is_on_hand_minus_live_holds_minus_the_safety_buffer(
    seeded_db: Path,
) -> None:
    """The formula, stated as a test so a later refactor cannot quietly change it."""
    set_stock(CAKE, on_hand=10, safety_buffer=2)
    insert_hold(CAKE, 3, status="active")

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.status == "known"
    assert result.quantity == 5  # 10 - 3 - 2
    assert result.on_hand == 10
    assert result.committed == 3
    assert result.safety_buffer == 2


def test_holds_awaiting_approval_also_decrement_availability(seeded_db: Path) -> None:
    """The case that prevents one cake being promised to two people.

    A large order sitting in the approval queue has not been promised yet, but it
    has been quoted. If it did not count against stock, a manager could approve it
    at the same moment a new caller is told those units are free. Counting it means
    the worst outcome is a decline, which costs one phone call.
    """
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    insert_hold(CAKE, 4, status="pending_approval")

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.quantity == 5  # 10 - 4 - 1
    assert result.committed == 4


def test_resolved_holds_stop_decrementing_availability(seeded_db: Path) -> None:
    """Collected, expired, cancelled and declined holds release their stock.

    All four are tested together because the bug they guard against is a single
    missing status in one `IN` clause, and that bug would only show up for whichever
    status was forgotten.
    """
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    for status in ("collected", "expired", "cancelled", "declined"):
        insert_hold(CAKE, 2, status=status)

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.committed == 0
    assert result.quantity == 9  # 10 - 0 - 1


def test_availability_is_never_negative(seeded_db: Path) -> None:
    """Oversell is reachable in reality, and must not produce a negative promise.

    The counter sells to walk-ins who never called, so physical stock genuinely can
    fall below what holds promised. A negative "sellable" would be meaningless to a
    caller and would break arithmetic upstream, so the floor is zero.
    """
    set_stock(CAKE, on_hand=2, safety_buffer=1)
    insert_hold(CAKE, 5, status="active")

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.status == "known"
    assert result.quantity == 0


def test_an_item_at_its_buffer_is_sellable_zero_not_unavailable(seeded_db: Path) -> None:
    """Sold out and unknown are different answers, and this one is sold out."""
    set_stock(CAKE, on_hand=1, safety_buffer=1)

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.status == "known"
    assert result.quantity == 0


def test_an_item_the_bakery_does_not_carry_raises_rather_than_returning_zero(
    seeded_db: Path,
) -> None:
    """A third answer, distinct from sold out and from unknown: we don't make that."""
    with pytest.raises(ItemNotFoundError):
        availability.sellable(LOCATION_ID, "cake_wedding_five_tier")


def test_the_known_result_carries_a_quantity_and_an_as_of_instant(seeded_db: Path) -> None:
    """A quantity without the instant it was true at is the representation to avoid."""
    payload = availability.sellable(LOCATION_ID, CAKE).as_dict()

    assert payload["status"] == "known"
    assert isinstance(payload["quantity"], int)
    assert payload["as_of"] is not None
    assert payload["item_name"]


def test_availability_for_the_whole_catalogue_covers_every_item(seeded_db: Path) -> None:
    """The dashboard reads every item in one call, ordered for display."""
    results = availability.sellable_all(LOCATION_ID)

    assert len(results) == 12
    assert all(r.status in {"known", "unknown"} for r in results)
    assert {r.item_id for r in results} >= {CAKE, CROISSANT}


def test_the_whole_catalogue_path_uses_the_same_formula_as_the_single_item_path(
    seeded_db: Path,
) -> None:
    """Two code paths computing availability differently would be the worst bug here.

    One would feed the dashboard and the other would feed the caller, and they would
    disagree on screen during a demo. They share one SQL statement; this proves it.
    """
    set_stock(CAKE, on_hand=7, safety_buffer=2)
    insert_hold(CAKE, 1, status="pending_approval")

    single = availability.sellable(LOCATION_ID, CAKE)
    from_catalogue = next(r for r in availability.sellable_all(LOCATION_ID) if r.item_id == CAKE)

    assert single.quantity == from_catalogue.quantity == 4


def test_the_committing_statuses_match_what_the_query_actually_counts() -> None:
    """The Python constant and the SQL must agree, or availability is silently wrong.

    `COMMITTING_STATUSES` is bound as parameters into the availability query, so a
    mismatch cannot happen through a hardcoded literal -- but the number of
    placeholders is generated from its length, and this asserts that relationship
    directly rather than trusting it.
    """
    placeholder_count = availability._AVAILABILITY_SQL.count("?")

    # One placeholder per committing status, plus one for location_id.
    assert placeholder_count == len(availability.COMMITTING_STATUSES) + 1
    assert availability.COMMITTING_STATUSES == ("active", "pending_approval")
