"""Exactly one caller gets the last cake.

Real threads, a real barrier, a real database file. Never a sequential simulation:
calling `create_hold` ten times in a row would pass even if the availability check
sat outside the transaction, which is the exact bug these tests exist to catch.

Every scenario is repeated, because a race condition that fails one run in twenty
is still a race condition. Each repetition is a separate pytest case so that a
failure names which run lost.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from holdline import db, holds

from helpers import (
    CAKE,
    LOCATION_ID,
    count_holds,
    count_live_holds,
    insert_hold,
    set_stock,
)

#: How many callers collide. Ten is well past the three phone lines a bakery has,
#: which is the point: the guarantee should not depend on the arrival rate.
RACERS = 10

#: A race that fails one run in twenty is still a race.
REPEATS = 20


def _race(attempt: int, *, quantity: int) -> list[holds.HoldResult | BaseException]:
    """Release `RACERS` threads simultaneously, each trying to hold `quantity`.

    Returns whatever each thread produced, including exceptions. Exceptions are
    collected rather than raised so that a failure surfaces as an assertion about
    the ledger rather than as a thread crash nobody sees.
    """
    barrier = threading.Barrier(RACERS)
    results: queue.Queue[holds.HoldResult | BaseException] = queue.Queue()

    def caller(index: int) -> None:
        try:
            # Every thread waits here, so they all attempt the write inside the
            # same few microseconds. Without the barrier, thread start-up latency
            # would space them out and hide the race.
            barrier.wait(timeout=10)
            results.put(
                holds.create_hold(
                    LOCATION_ID,
                    CAKE,
                    quantity,
                    customer_name=f"caller-{attempt}-{index}",
                )
            )
        except BaseException as exc:  # broad on purpose: reported as data, not swallowed
            results.put(exc)
        finally:
            # Each thread opened its own connection; close it here so twenty
            # repetitions do not accumulate two hundred open handles.
            db.close_connection()

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    return [results.get_nowait() for _ in range(results.qsize())]


@pytest.mark.parametrize("attempt", range(REPEATS))
def test_only_one_caller_gets_the_last_cake(seeded_db: Path, attempt: int) -> None:
    """With one sellable unit and ten simultaneous callers, exactly one succeeds.

    The scenario is a busy afternoon: twelve cakes on the shelf, ten already
    promised to earlier callers, one safety buffer held back. That leaves exactly
    one unit sellable, and it is a realistic way to arrive there -- the winner's
    hold activates immediately rather than needing approval, because taking the
    eleventh of twelve physical cakes does not touch the reserve.

    Asserts the three things that matter: one success, nine refusals that say
    "insufficient" rather than crashing, and a ledger holding exactly one new
    hold. The third is the real assertion -- the first two could both be true while
    the database contained two holds, if the check sat outside the transaction.
    """
    set_stock(CAKE, on_hand=12, safety_buffer=1)
    for _ in range(10):
        insert_hold(CAKE, 1, status="active")

    outcomes = _race(attempt, quantity=1)

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert not failures, f"threads raised instead of being refused: {failures}"

    granted = [o for o in outcomes if isinstance(o, holds.HoldResult) and o.ok]
    refused = [o for o in outcomes if isinstance(o, holds.HoldResult) and not o.ok]

    assert len(granted) == 1, f"expected exactly one winner, got {len(granted)}"
    assert len(refused) == RACERS - 1
    assert all(r.reason == "insufficient" for r in refused)

    assert granted[0].status == "active"
    assert count_holds(status="active", item_id=CAKE) == 11  # the ten arranged, plus one


@pytest.mark.parametrize("attempt", range(REPEATS))
def test_the_last_physical_unit_is_promised_to_exactly_one_caller(
    seeded_db: Path,
    attempt: int,
) -> None:
    """The same guarantee when the scarcity is physical rather than from holds.

    Two cakes on the shelf and a buffer of one, so one unit is sellable and filling
    it takes the shelf down to the reserve. That trips the `stock_at_safety_buffer`
    policy, so the winning hold lands in the approval queue instead of activating.

    The assertion is therefore about *live* holds -- active plus pending_approval --
    because both reserve stock. Counting only `active` here would report zero
    winners for a hold that was correctly routed to a human, which would be a test
    asserting the opposite of the intended behaviour.
    """
    set_stock(CAKE, on_hand=2, safety_buffer=1)

    outcomes = _race(attempt, quantity=1)

    granted = [o for o in outcomes if isinstance(o, holds.HoldResult) and o.ok]
    refused = [o for o in outcomes if isinstance(o, holds.HoldResult) and not o.ok]

    assert len(granted) == 1
    assert granted[0].status == "pending_approval"
    assert "stock_at_safety_buffer" in granted[0].approval_reasons
    assert len(refused) == RACERS - 1
    assert count_live_holds(CAKE) == 1


@pytest.mark.parametrize("attempt", range(5))
def test_multi_unit_requests_never_oversell(seeded_db: Path, attempt: int) -> None:
    """Five sellable units and ten callers each wanting two: exactly two win.

    The unit-quantity case can pass on a lucky implementation that happens to
    serialise. This one cannot: the ledger has to subtract the right *amount*, not
    merely notice that something was taken, and the sum of everything granted must
    never exceed what was available.
    """
    set_stock(CAKE, on_hand=9, safety_buffer=1)
    for _ in range(3):
        insert_hold(CAKE, 1, status="active")
    # sellable = 9 on hand - 3 held - 1 buffer = 5

    outcomes = _race(attempt, quantity=2)

    granted = [o for o in outcomes if isinstance(o, holds.HoldResult) and o.ok]
    total_granted = sum(g.quantity or 0 for g in granted)

    assert len(granted) == 2, f"expected two winners, got {len(granted)}"
    assert total_granted == 4
    assert total_granted <= 5, "the ledger promised more than was sellable"


def test_a_losing_caller_is_refused_rather_than_told_the_database_is_locked(
    seeded_db: Path,
) -> None:
    """Contention must surface as a business answer, never as a database error.

    `busy_timeout` is what makes this true: a thread that cannot get the write lock
    waits for it, re-reads availability, and is told no. Without it, nine callers
    would receive "database is locked", which is not something an assistant can say
    to a caller.
    """
    set_stock(CAKE, on_hand=2, safety_buffer=1)

    outcomes = _race(0, quantity=1)

    assert all(isinstance(o, holds.HoldResult) for o in outcomes)
    refused = [o for o in outcomes if isinstance(o, holds.HoldResult) and not o.ok]
    assert {r.reason for r in refused} == {"insufficient"}
