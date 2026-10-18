"""The test that matters: only one caller gets the last cake."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from holdline import clock, inventory
from holdline.holds import create_hold

THREADS = 10
REPETITIONS = 20


def _race(db, location_id: str, item_id: str) -> list:
    """Fire THREADS create_hold calls released together by a barrier."""
    barrier = threading.Barrier(THREADS)
    results: list = [None] * THREADS
    pickup = clock.now() + timedelta(hours=1)

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = create_hold(
            db,
            location_id=location_id,
            item_id=item_id,
            quantity=1,
            pickup_at=pickup,
            call_id=f"caller-{i}",
            channel="browser",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


@pytest.mark.parametrize("repetition", range(REPETITIONS))
def test_only_one_caller_gets_the_last_cake(db, location_id, repetition):
    """
    Stock set to exactly 1 sellable unit. Ten threads released simultaneously by a
    barrier all attempt create_hold.

    Asserts: exactly 1 succeeds, 9 fail with reason "insufficient", and the ledger
    contains exactly 1 active hold.

    Repeated 20 times, because a race condition that fails 1 run in 20 is still a
    race condition.
    """
    # buffer for cake_choc_9 is 1, so on_hand 2 means exactly 1 sellable.
    inventory.set_stock(db, location_id, "cake_choc_9", 2)
    with db.transaction(immediate=True) as conn:
        conn.execute("DELETE FROM holds")

    results = _race(db, location_id, "cake_choc_9")

    assert all(r is not None for r in results), "a thread did not finish"
    winners = [r for r in results if r.ok]
    losers = [r for r in results if not r.ok]
    assert len(winners) == 1, f"repetition {repetition}: {len(winners)} winners"
    assert len(losers) == THREADS - 1
    assert all(r.reason == "insufficient" for r in losers)
    assert winners[0].status == "active"
    with db.connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM holds WHERE status = 'active'").fetchone()[0]
    assert n == 1
