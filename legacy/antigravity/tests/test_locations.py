"""The location record, and the one place local time is produced."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from holdline import holds, locations
from holdline.exceptions import LocationNotFoundError
from holdline.timeutils import local_wall_time_to_utc, utc_now

from helpers import CAKE, LOCATION_ID, alerts_of_kind, set_stock


def test_the_seeded_location_is_readable(seeded_db: Path) -> None:
    """Phase 3's store-info tool answers hours, address and phone from this row."""
    location = locations.get_location()

    assert location.id == LOCATION_ID
    assert location.timezone == "America/Chicago"
    assert location.address is not None


def test_a_missing_location_raises_rather_than_returning_a_default(temp_db: Path) -> None:
    """An unseeded database must fail loudly, not answer from thin air.

    Returning a placeholder location would let the assistant confidently give a
    caller an address that does not exist.
    """
    with pytest.raises(LocationNotFoundError, match="seeded"):
        locations.get_location()


def test_a_pickup_later_today_reads_as_a_bare_clock_time(seeded_db: Path) -> None:
    """Staff think in clock time like 5:22pm, while the stored instant is UTC."""
    # 5:22pm local, whatever today's UTC offset happens to be.
    local_five = local_wall_time_to_utc(
        utc_now().astimezone().replace(hour=17, minute=22, second=0, microsecond=0, tzinfo=None),
        "America/Chicago",
    )

    label = locations.local_label(local_five)

    assert label.endswith("pm")
    assert ":22" in label


def test_the_leading_zero_is_stripped_from_the_hour(seeded_db: Path) -> None:
    """Nobody says "05:22pm", and the %-I that strips the pad is absent on Windows."""
    morning = local_wall_time_to_utc(
        utc_now().astimezone().replace(hour=9, minute=5, second=0, microsecond=0, tzinfo=None),
        "America/Chicago",
    )

    label = locations.local_label(morning)

    assert label.startswith("9:05")


def test_a_pickup_on_another_day_carries_its_weekday(seeded_db: Path) -> None:
    """A pickup time with no day is how a customer arrives twenty-four hours early."""
    tomorrow = utc_now() + timedelta(days=1)

    label = locations.local_label(tomorrow)

    # Three-letter weekday, then the clock time.
    assert len(label.split(" ")) == 2
    assert label.split(" ")[0] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}


def test_a_staff_alert_shows_the_pickup_time_in_shop_local_time(seeded_db: Path) -> None:
    """The reason this module exists.

    The ledger stores UTC. An approval queue that printed "pickup 22:22" for a
    5:22pm collection would be misread every afternoon, so the summary a human
    reads is rendered in the shop's timezone.
    """
    set_stock(CAKE, on_hand=30, safety_buffer=1)

    holds.create_hold(LOCATION_ID, CAKE, 5, customer_name="Marcus")

    summary = str(alerts_of_kind("approval_needed")[0]["summary"])
    assert "pickup" in summary
    # No UTC ISO timestamp leaks into the human-readable line.
    assert "+00:00" not in summary
    assert "T" not in summary.split("pickup")[1]
