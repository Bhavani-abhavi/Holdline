"""Expired holds release their stock, whatever state they were in."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from holdline import availability, expiry, holds

from helpers import (
    CAKE,
    LOCATION_ID,
    alerts_of_kind,
    count_live_holds,
    events_of_kind,
    hold_status,
    insert_hold,
    on_hand,
    set_stock,
)


def test_an_expired_hold_releases_its_stock_to_the_next_caller(seeded_db: Path) -> None:
    """The point of expiry: a customer who did not come stops blocking one who has."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    hold_id = insert_hold(CAKE, 3, status="active", expires_in_minutes=-5)
    assert availability.sellable(LOCATION_ID, CAKE).quantity == 6

    released = holds.expire_due_holds()

    assert released == 1
    assert hold_status(hold_id) == "expired"
    assert availability.sellable(LOCATION_ID, CAKE).quantity == 9


def test_an_unanswered_approval_request_also_expires(seeded_db: Path) -> None:
    """The gap the original specification left open.

    Pending-approval holds decrement availability, which is correct. Without expiry
    covering them, a manager who never clicks Approve or Decline would take those
    units off the line permanently -- an eight-cake request nobody answered would
    silently starve every later caller.
    """
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    hold_id = insert_hold(CAKE, 8, status="pending_approval", expires_in_minutes=-1)
    assert availability.sellable(LOCATION_ID, CAKE).quantity == 1

    released = holds.expire_due_holds()

    assert released == 1
    assert hold_status(hold_id) == "expired"
    assert availability.sellable(LOCATION_ID, CAKE).quantity == 9


def test_expiry_does_not_touch_holds_that_are_still_within_their_window(
    seeded_db: Path,
) -> None:
    """A sweep runs every twenty seconds, so it must be precise about what is due."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    live = insert_hold(CAKE, 1, status="active", expires_in_minutes=30)
    due = insert_hold(CAKE, 1, status="active", expires_in_minutes=-1)

    released = holds.expire_due_holds()

    assert released == 1
    assert hold_status(live) == "active"
    assert hold_status(due) == "expired"


def test_expiry_leaves_already_resolved_holds_alone(seeded_db: Path) -> None:
    """A collected hold is history, and rewriting it would corrupt the audit trail.

    All four resolved statuses are arranged with past expiry timestamps, because the
    bug this guards against is a `WHERE` clause that filters on time but forgets to
    filter on status.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    ids = {
        status: insert_hold(CAKE, 1, status=status, expires_in_minutes=-60)
        for status in ("collected", "cancelled", "declined", "expired")
    }

    released = holds.expire_due_holds()

    assert released == 0
    for status, hold_id in ids.items():
        assert hold_status(hold_id) == status


def test_running_the_sweep_twice_changes_nothing_the_second_time(seeded_db: Path) -> None:
    """Idempotent, because a background worker will run it thousands of times a day."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    insert_hold(CAKE, 2, status="active", expires_in_minutes=-5)

    first = holds.expire_due_holds()
    second = holds.expire_due_holds()

    assert first == 1
    assert second == 0
    assert len(events_of_kind("hold_expired")) == 1


def test_expiry_records_an_event_and_tells_staff(seeded_db: Path) -> None:
    """Stock reappearing on the board needs an explanation, or it looks like a bug."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    insert_hold(CAKE, 2, status="active", expires_in_minutes=-5)

    holds.expire_due_holds()

    assert len(events_of_kind("hold_expired")) == 1
    alerts = alerts_of_kind("hold_expired")
    assert len(alerts) == 1
    assert "released" in str(alerts[0]["summary"])


def test_an_expired_approval_request_says_so_in_the_alert(seeded_db: Path) -> None:
    """Staff should be able to tell neglect from a no-show, since one is their fault."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    insert_hold(CAKE, 5, status="pending_approval", expires_in_minutes=-1)

    holds.expire_due_holds()

    alerts = alerts_of_kind("hold_expired")
    assert "never approved" in str(alerts[0]["summary"])


def test_expiry_does_not_change_physical_stock(seeded_db: Path) -> None:
    """Nothing left the shelf, so on-hand must not move. Only the promise ended."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    insert_hold(CAKE, 3, status="active", expires_in_minutes=-5)

    holds.expire_due_holds()

    assert on_hand(CAKE) == 10


def test_many_due_holds_are_all_released_in_one_sweep(seeded_db: Path) -> None:
    """A process that was down for an hour must catch up on its first sweep.

    This is the argument for a poller over a timer per hold: work defined by state
    recovers after a restart, whereas a queue of missed ticks is simply lost.
    """
    set_stock(CAKE, on_hand=30, safety_buffer=1)
    for _ in range(7):
        insert_hold(CAKE, 1, status="active", expires_in_minutes=-30)

    released = holds.expire_due_holds()

    assert released == 7
    assert count_live_holds(CAKE) == 0


def test_the_worker_sweeps_and_then_stops_cleanly(seeded_db: Path) -> None:
    """The background loop must do its job and shut down without being cancelled.

    Shutdown waits on an event rather than sleeping, so stopping is immediate instead
    of taking up to a full sweep interval -- which matters for a graceful server
    shutdown.
    """
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    hold_id = insert_hold(CAKE, 2, status="active", expires_in_minutes=-5)

    async def run_briefly() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(expiry.run_expiry_loop(interval_seconds=1, stop=stop))
        # One sweep happens immediately on entry to the loop; wait for it to land.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if await asyncio.to_thread(hold_status, hold_id) == "expired":
                break
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run_briefly())

    assert hold_status(hold_id) == "expired"


def test_a_failing_sweep_does_not_kill_the_worker(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad sweep must not take the worker down.

    The work is defined by state rather than by a queue of ticks, so the next sweep
    twenty seconds later catches up. A worker that died on the first transient lock
    would leave every later hold reserved forever.
    """
    calls: list[int] = []

    def flaky() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated transient database error")
        return 0

    monkeypatch.setattr(expiry, "sweep_once", flaky)

    async def run_twice() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(expiry.run_expiry_loop(interval_seconds=1, stop=stop))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if len(calls) >= 2:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run_twice())

    assert len(calls) >= 2, "the worker stopped after the failing sweep"
