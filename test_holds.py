"""The hold ledger: what gets promised, what needs a human, and what is refused."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from holdline import availability, config, holds
from holdline.timeutils import utc_now

from helpers import (
    CAKE,
    CROISSANT,
    LOCATION_ID,
    alerts_of_kind,
    count_holds,
    events_of_kind,
    hold_status,
    insert_hold,
    on_hand,
    set_stock,
)


def test_a_small_order_with_plenty_of_stock_is_held_immediately(seeded_db: Path) -> None:
    """The happy path: nothing about this needs a person, so nobody is interrupted."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    result = holds.create_hold(
        LOCATION_ID, CAKE, 1, customer_name="Dana", customer_phone="+13125550188"
    )

    assert result.ok is True
    assert result.status == "active"
    assert result.approval_reasons == ()
    assert result.hold_id is not None


def test_a_hold_reduces_what_the_next_caller_is_offered(seeded_db: Path) -> None:
    """The whole product in one test: a promise changes what everyone else is told."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    before = availability.sellable(LOCATION_ID, CAKE).quantity

    holds.create_hold(LOCATION_ID, CAKE, 2)
    after = availability.sellable(LOCATION_ID, CAKE).quantity

    assert before == 9
    assert after == 7


def test_a_quantity_above_the_auto_approve_ceiling_waits_for_a_human(seeded_db: Path) -> None:
    """Three or more is an event or a mistake. Both want a person's eyes."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)

    result = holds.create_hold(LOCATION_ID, CAKE, config.MAX_AUTO_APPROVE_QTY + 1)

    assert result.status == "pending_approval"
    assert "quantity_above_auto_approve" in result.approval_reasons


def test_a_pickup_beyond_one_shift_waits_for_a_human(seeded_db: Path) -> None:
    """The staff on duty at pickup are not the staff who took the call."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    far_off = utc_now() + timedelta(hours=config.MAX_HOLD_HOURS_AHEAD + 1)

    result = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=far_off)

    assert result.status == "pending_approval"
    assert "pickup_too_far_ahead" in result.approval_reasons


def test_taking_stock_down_to_the_reserve_waits_for_a_human(seeded_db: Path) -> None:
    """The safety-buffer rule, evaluated against the stock this hold would leave behind.

    Read as a statement about stock *before* the hold, the rule can never fire: a
    hold only succeeds when `on_hand - committed - buffer >= quantity`, which already
    implies `on_hand > buffer`. Evaluated against the resulting shelf it fires
    exactly when it should -- when filling this order empties the reserve.
    """
    set_stock(CAKE, on_hand=2, safety_buffer=1)

    result = holds.create_hold(LOCATION_ID, CAKE, 1)

    assert result.status == "pending_approval"
    assert "stock_at_safety_buffer" in result.approval_reasons


def test_every_applicable_reason_is_reported_not_just_the_first(seeded_db: Path) -> None:
    """Staff should read "8 cakes, six hours out, and a nut allergy", not one of the three."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)

    result = holds.create_hold(
        LOCATION_ID,
        CAKE,
        8,
        pickup_at=utc_now() + timedelta(hours=config.MAX_HOLD_HOURS_AHEAD + 2),
        dietary_note="severe nut allergy",
    )

    assert set(result.approval_reasons) == {
        "quantity_above_auto_approve",
        "pickup_too_far_ahead",
        "dietary_note_present",
    }


def test_the_reason_a_hold_needed_approval_is_stored_on_the_hold(seeded_db: Path) -> None:
    """The alert gets acknowledged and ages out; the hold is the lasting record."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)

    result = holds.create_hold(LOCATION_ID, CAKE, 5)
    stored = holds.get_hold(result.hold_id or 0)

    assert stored is not None
    assert "quantity_above_auto_approve" in (stored["approval_reason"] or "")


def test_an_active_hold_expires_a_grace_window_after_the_stated_pickup(seeded_db: Path) -> None:
    """Forty-five minutes past the promise, not forty-five minutes from now."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    pickup = utc_now() + timedelta(hours=1)

    result = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup)

    assert result.expires_at is not None
    expected = pickup + timedelta(minutes=config.DEFAULT_HOLD_MINUTES)
    assert abs((result.expires_at - expected).total_seconds()) < 2


def test_a_hold_awaiting_approval_expires_on_the_approval_deadline_instead(
    seeded_db: Path,
) -> None:
    """Unanswered approvals must not reserve stock indefinitely.

    This is the gap the original specification left: pending holds decrement
    availability, so a manager who never clicks anything would take stock off the
    line permanently. The pending hold therefore expires on its own short TTL rather
    than on the pickup window.
    """
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    pickup = utc_now() + timedelta(hours=3)

    result = holds.create_hold(LOCATION_ID, CAKE, 5, pickup_at=pickup)

    assert result.expires_at is not None
    ttl_deadline = utc_now() + timedelta(minutes=config.PENDING_APPROVAL_TTL_MINUTES)
    assert abs((result.expires_at - ttl_deadline).total_seconds()) < 5
    assert result.expires_at < pickup


def test_approving_a_hold_extends_it_to_the_real_pickup_window(seeded_db: Path) -> None:
    """Without this, an approved hold could expire minutes after the manager clicked.

    The approval TTL is short by design, so approval has to recompute the expiry
    from the stored pickup time -- which is why `pickup_at` is a column rather than a
    value used once and discarded.
    """
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    pickup = utc_now() + timedelta(hours=3)
    created = holds.create_hold(LOCATION_ID, CAKE, 5, pickup_at=pickup)

    approved = holds.approve_hold(created.hold_id or 0)

    assert approved.ok is True
    assert approved.status == "active"
    assert approved.expires_at is not None
    expected = pickup + timedelta(minutes=config.DEFAULT_HOLD_MINUTES)
    assert abs((approved.expires_at - expected).total_seconds()) < 2


def test_approving_the_same_hold_twice_does_nothing_the_second_time(seeded_db: Path) -> None:
    """The staff page gets double-clicked and requests get retried.

    The guard is `WHERE ... AND status IN (...)`, so the second attempt matches zero
    rows and is reported rather than applied again.
    """
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    created = holds.create_hold(LOCATION_ID, CAKE, 5)

    first = holds.approve_hold(created.hold_id or 0)
    second = holds.approve_hold(created.hold_id or 0)

    assert first.ok is True
    assert second.ok is False
    assert second.status == "active"  # reports what it actually is


def test_declining_a_hold_releases_its_stock_to_the_next_caller(seeded_db: Path) -> None:
    """A declined hold must stop counting immediately, not at expiry."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    created = holds.create_hold(LOCATION_ID, CAKE, 4)
    assert availability.sellable(LOCATION_ID, CAKE).quantity == 5

    holds.decline_hold(created.hold_id or 0)

    assert availability.sellable(LOCATION_ID, CAKE).quantity == 9
    assert hold_status(created.hold_id or 0) == "declined"


def test_cancelling_works_from_both_live_states(seeded_db: Path) -> None:
    """A caller can change their mind before or after a manager has decided."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    pending = holds.create_hold(LOCATION_ID, CAKE, 5)
    active = holds.create_hold(LOCATION_ID, CAKE, 1)

    assert holds.cancel_hold(pending.hold_id or 0).ok is True
    assert holds.cancel_hold(active.hold_id or 0).ok is True
    assert hold_status(pending.hold_id or 0) == "cancelled"
    assert hold_status(active.hold_id or 0) == "cancelled"


def test_collecting_a_hold_records_the_matching_sale(seeded_db: Path) -> None:
    """The customer took the cake, so the shelf count has to drop.

    This is also the invariant the Phase 8 marts check: every collected hold has a
    matching sale in its window. Availability is unchanged by collection -- the hold
    stops reserving at the same moment the stock leaves -- and that net-zero result
    is the interesting part.
    """
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    created = holds.create_hold(LOCATION_ID, CAKE, 2)
    available_while_held = availability.sellable(LOCATION_ID, CAKE).quantity

    holds.collect_hold(created.hold_id or 0)

    assert on_hand(CAKE) == 8
    assert availability.sellable(LOCATION_ID, CAKE).quantity == available_while_held
    assert len(events_of_kind("sale")) == 1


def test_a_hold_awaiting_approval_cannot_be_collected(seeded_db: Path) -> None:
    """Nobody has agreed to it yet, so there is nothing to hand over."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    created = holds.create_hold(LOCATION_ID, CAKE, 5)

    result = holds.collect_hold(created.hold_id or 0)

    assert result.ok is False
    assert hold_status(created.hold_id or 0) == "pending_approval"
    assert on_hand(CAKE) == 30


@pytest.mark.parametrize("quantity", [0, -1, -50])
def test_a_nonsense_quantity_is_refused_without_touching_the_ledger(
    seeded_db: Path,
    quantity: int,
) -> None:
    """Refused as data, not as an exception: the assistant has to say this out loud."""
    result = holds.create_hold(LOCATION_ID, CAKE, quantity)

    assert result.ok is False
    assert result.reason == "invalid_quantity"


def test_a_pickup_time_already_in_the_past_is_refused(seeded_db: Path) -> None:
    """A past pickup means the spoken time was resolved against the wrong day.

    Holding stock that expires before the caller has hung up is worse than saying so.
    """
    result = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=utc_now() - timedelta(hours=1))

    assert result.ok is False
    assert result.reason == "pickup_in_past"


def test_an_item_the_bakery_does_not_carry_is_refused_by_name(seeded_db: Path) -> None:
    """An uncarried item is its own answer, and create_hold never raises for it."""
    result = holds.create_hold(LOCATION_ID, "cake_wedding_five_tier", 1)

    assert result.ok is False
    assert result.reason == "unknown_item"


def test_a_request_for_more_than_is_sellable_is_refused_with_what_is_left(
    seeded_db: Path,
) -> None:
    """The refusal carries the real number so the assistant can offer an alternative."""
    set_stock(CAKE, on_hand=4, safety_buffer=1)

    result = holds.create_hold(LOCATION_ID, CAKE, 10)

    assert result.ok is False
    assert result.reason == "insufficient"
    assert result.sellable_remaining == 3


def test_a_refusal_is_written_to_the_event_log(seeded_db: Path) -> None:
    """Turning someone away because we were out is a demand signal, not just a log line.

    It is unanswerable unless the refusal was recorded when it happened. The Phase 8
    stockout analysis reads exactly these rows.
    """
    set_stock(CAKE, on_hand=1, safety_buffer=1)

    holds.create_hold(LOCATION_ID, CAKE, 1)

    refusals = events_of_kind("hold_refused")
    assert len(refusals) == 1


def test_the_live_hold_list_includes_holds_awaiting_approval(seeded_db: Path) -> None:
    """A staff view that hid pending holds could not explain why availability is low."""
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    holds.create_hold(LOCATION_ID, CAKE, 5)  # pending
    holds.create_hold(LOCATION_ID, CAKE, 1)  # active

    with_pending = holds.get_active_holds(LOCATION_ID, include_pending=True)
    without_pending = holds.get_active_holds(LOCATION_ID, include_pending=False)

    assert len(with_pending) == 2
    assert len(without_pending) == 1


def test_a_hold_never_puts_a_phone_number_into_the_event_log(seeded_db: Path) -> None:
    """The event log is exported to Parquet and read by analytics; the ledger is not.

    Staff who need to dial a customer read the hold row. Everything downstream of
    the event log sees a redacted form.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    holds.create_hold(LOCATION_ID, CAKE, 1, customer_phone="+13125550188")

    created = events_of_kind("hold_created")
    assert len(created) == 1
    assert "3125550188" not in str(created[0]["detail"])
    assert "0188" in str(created[0]["detail"])  # last four kept for recognisability


def test_holds_on_different_items_do_not_affect_each_other(seeded_db: Path) -> None:
    """Availability is per item; a run on croissants must not shrink the cake count."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    set_stock(CROISSANT, on_hand=10, safety_buffer=2)
    insert_hold(CROISSANT, 5, status="active")

    assert availability.sellable(LOCATION_ID, CAKE).quantity == 9
    assert availability.sellable(LOCATION_ID, CROISSANT).quantity == 3


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_the_same_request_twice_in_one_call_produces_one_hold(seeded_db: Path) -> None:
    """A live model was observed calling create_hold twice and cancelling the duplicate.

    Correctness must not depend on the model noticing its own mistake. The second call
    returns the hold that already exists, and the ledger has exactly one row.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    pickup = utc_now() + timedelta(minutes=20)

    first = holds.create_hold(
        LOCATION_ID, CAKE, 1, pickup_at=pickup, customer_name="Dana", call_id="call_1"
    )
    second = holds.create_hold(
        LOCATION_ID, CAKE, 1, pickup_at=pickup, customer_name="Dana", call_id="call_1"
    )

    assert first.ok is True
    assert second.ok is True
    assert second.hold_id == first.hold_id
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert count_holds(item_id=CAKE) == 1


def test_deduplication_does_not_double_count_against_availability(seeded_db: Path) -> None:
    """The point of the fix: a duplicate must not reserve the cake twice.

    Two holds of one unit would take two units off the line while the caller was promised
    one, so a later caller is refused stock that is not actually committed.
    """
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    pickup = utc_now() + timedelta(minutes=20)

    holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_1")
    holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_1")

    assert availability.sellable(LOCATION_ID, CAKE).quantity == 8  # 10 - 1 - 1 buffer


def test_a_pickup_time_resolved_microseconds_apart_still_deduplicates(
    seeded_db: Path,
) -> None:
    """The key truncates to the minute, which is what makes this work at all.

    Two calls in one turn resolve "in twenty minutes" to instants microseconds apart. A
    key built from the raw instant would differ and dedupe nothing -- precisely the
    failure it exists to prevent.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    base = utc_now() + timedelta(minutes=20)

    first = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=base, call_id="call_1")
    second = holds.create_hold(
        LOCATION_ID,
        CAKE,
        1,
        pickup_at=base + timedelta(microseconds=900),
        call_id="call_1",
    )

    assert second.hold_id == first.hold_id
    assert count_holds(item_id=CAKE) == 1


def test_a_different_quantity_is_a_different_order(seeded_db: Path) -> None:
    """Deduplication must not swallow a caller changing their mind upward."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    pickup = utc_now() + timedelta(minutes=20)

    one = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_1")
    two = holds.create_hold(LOCATION_ID, CAKE, 2, pickup_at=pickup, call_id="call_1")

    assert two.hold_id != one.hold_id
    assert count_holds(item_id=CAKE) == 2


def test_two_different_callers_are_never_deduplicated(seeded_db: Path) -> None:
    """The key is scoped to a call. Two people wanting the same cake are two orders."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    pickup = utc_now() + timedelta(minutes=20)

    first = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_1")
    second = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_2")

    assert second.hold_id != first.hold_id
    assert second.deduplicated is False
    assert count_holds(item_id=CAKE) == 2


def test_a_hold_with_no_call_id_is_never_deduplicated(seeded_db: Path) -> None:
    """Without a call there is no scope for the key.

    A staff-page hold and a walk-in hold placed a second apart are two genuine orders,
    and collapsing them would lose one.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    pickup = utc_now() + timedelta(minutes=20)

    holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup)
    holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup)

    assert count_holds(item_id=CAKE) == 2


def test_ordering_the_same_thing_again_after_collecting_is_a_new_order(
    seeded_db: Path,
) -> None:
    """The unique index is partial, so the constraint only applies while a hold is live.

    A caller who collects and then rings back for another is not a duplicate.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    pickup = utc_now() + timedelta(minutes=20)
    first = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_1")
    holds.collect_hold(first.hold_id or 0)

    again = holds.create_hold(LOCATION_ID, CAKE, 1, pickup_at=pickup, call_id="call_1")

    assert again.hold_id != first.hold_id
    assert again.deduplicated is False


def test_collecting_a_hold_queues_the_review_request(seeded_db: Path) -> None:
    """Asked after the customer has the cake, not when the hold was made.

    Raised unconditionally, for every collection. That is why it is a call site rather
    than a prompt instruction: sentiment-gated review solicitation violates Google's
    policies, and a prompt asking the model not to do it is a request, not a guarantee.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    created = holds.create_hold(LOCATION_ID, CAKE, 1, call_id="call_1")

    assert alerts_of_kind("review_request") == []

    holds.collect_hold(created.hold_id or 0)

    requests = alerts_of_kind("review_request")
    assert len(requests) == 1
    assert requests[0]["urgency"] == "info"


def test_a_cancelled_hold_gets_no_review_request(seeded_db: Path) -> None:
    """Nothing was collected, so there is nothing to review."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    created = holds.create_hold(LOCATION_ID, CAKE, 1, call_id="call_1")

    holds.cancel_hold(created.hold_id or 0)

    assert alerts_of_kind("review_request") == []
