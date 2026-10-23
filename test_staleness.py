"""When the POS sync is old, the system loses the ability to promise.

Not "warns about" -- loses. The distinction is the point of these tests: a stale
result carries no quantity at all, so there is no number anywhere downstream that
could be repeated to a caller.
"""

from __future__ import annotations

from pathlib import Path

from holdline import availability, config, holds, inventory
from holdline.timeutils import utc_now

from helpers import CAKE, LOCATION_ID, age_sync, events_of_kind, set_stock


def test_stale_data_returns_unknown_with_a_reason(seeded_db: Path) -> None:
    """Past the threshold, availability is unknown rather than approximate."""
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS + 60)

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.status == "unknown"
    assert result.reason == "stale_data"
    assert result.age_seconds is not None
    assert result.age_seconds > config.STALE_THRESHOLD_SECONDS


def test_no_quantity_leaks_out_of_a_stale_result(seeded_db: Path) -> None:
    """The guarantee. There must be no number to repeat, not merely an instruction not to.

    Checked on both the object and the dictionary the tool layer returns, because
    the dictionary is what actually reaches the model's context.
    """
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS * 2)

    result = availability.sellable(LOCATION_ID, CAKE)
    payload = result.as_dict()

    assert result.quantity is None
    assert result.on_hand is None
    assert result.committed is None
    assert "quantity" not in payload
    assert "on_hand" not in payload
    assert not any(
        isinstance(value, int) and value > 0
        for key, value in payload.items()
        if key != "age_seconds"
    )


def test_data_exactly_at_the_threshold_is_still_usable(seeded_db: Path) -> None:
    """The gate fires *past* the threshold, not at it.

    An off-by-one here would either refuse service a full threshold early or allow
    data a second too old. The boundary is asserted from both sides.
    """
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS)

    assert availability.sellable(LOCATION_ID, CAKE).status == "known"


def test_data_one_second_past_the_threshold_is_refused(seeded_db: Path) -> None:
    """The other side of the same boundary."""
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS + 1)

    assert availability.sellable(LOCATION_ID, CAKE).status == "unknown"


def test_a_future_sync_timestamp_is_treated_as_fresh_not_as_an_error(seeded_db: Path) -> None:
    """Clock skew must not take the phone line down.

    A sync timestamp in the future means a machine's clock disagrees with ours. The
    safe direction is to treat the data as current: refusing every caller because of
    a clock is a worse failure than trusting data that is, if anything, newer than
    we expected.
    """
    set_stock(CAKE, on_hand=8, synced_at=utc_now().replace(year=utc_now().year + 1))

    result = availability.sellable(LOCATION_ID, CAKE)

    assert result.status == "known"
    assert result.age_seconds is None


def test_a_hold_cannot_be_created_against_stale_data(seeded_db: Path) -> None:
    """The gate is not advisory: it blocks the write, not just the sentence.

    The refusal reason is `stale_data` rather than `insufficient`, because they are
    different conversations -- one is an apology, the other is an offer to call back.
    """
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS + 60)

    result = holds.create_hold(LOCATION_ID, CAKE, 1)

    assert result.ok is False
    assert result.reason == "stale_data"
    assert result.hold_id is None


def test_refusing_to_quote_is_recorded_as_a_business_event(seeded_db: Path) -> None:
    """How often we refused to quote because of stale data must be answerable later.

    It is unanswerable after the fact unless the refusal was written down when it
    happened, which is why this is an event rather than only a log line.
    """
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS + 60)

    availability.sellable(LOCATION_ID, CAKE)

    recorded = events_of_kind("availability_unknown")
    assert len(recorded) == 1


def test_breaking_the_pos_sync_stops_every_item_being_quoted(seeded_db: Path) -> None:
    """The demo control has to affect the whole catalogue, not one item."""
    inventory.force_stale(LOCATION_ID)

    results = availability.sellable_all(LOCATION_ID)

    assert all(r.status == "unknown" for r in results)


def test_restoring_the_sync_brings_quoting_back(seeded_db: Path) -> None:
    """Breaking the POS sync needs an un-break, or a demo cannot recover.

    Without this the only way back is to re-seed, which discards whatever holds the
    demo just created -- exactly the interesting state an interviewer is looking at.
    """
    inventory.force_stale(LOCATION_ID)
    assert availability.sellable(LOCATION_ID, CAKE).status == "unknown"

    inventory.restore_sync(LOCATION_ID)

    assert availability.sellable(LOCATION_ID, CAKE).status == "known"


def test_a_sale_refreshes_the_sync_and_makes_data_usable_again(seeded_db: Path) -> None:
    """A sale is also a sync: the register just told us what it knows.

    This mirrors the real APIs, whose timestamps advance when the register reports,
    and it means an active counter naturally keeps the phone line able to quote.
    """
    age_sync(CAKE, config.STALE_THRESHOLD_SECONDS + 60)
    assert availability.sellable(LOCATION_ID, CAKE).status == "unknown"

    inventory.record_sale(LOCATION_ID, CAKE, 1)

    assert availability.sellable(LOCATION_ID, CAKE).status == "known"
