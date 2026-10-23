"""Time handling. A promise with an ambiguous expiry time is not a promise."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from holdline.timeutils import (
    age_seconds,
    from_iso,
    local_wall_time_to_utc,
    to_iso,
    to_local,
    utc_now,
)


def test_a_naive_datetime_is_refused_rather_than_assumed_to_be_utc() -> None:
    """Assuming a timezone is wrong precisely when it matters most."""
    with pytest.raises(ValueError, match="naive datetime"):
        to_iso(datetime(2026, 9, 29, 17, 0))  # noqa: DTZ001 -- the point of the test


def test_a_timestamp_survives_a_round_trip_through_the_database_format() -> None:
    """Whole-second precision in, the same instant out."""
    original = datetime(2026, 9, 29, 21, 8, 34, tzinfo=UTC)

    assert from_iso(to_iso(original)) == original


def test_stored_timestamps_sort_chronologically_as_plain_strings() -> None:
    """Lexicographic order must equal chronological order.

    Every range scan in the application and every watermark query in Phase 8
    relies on this, and it is a property of the chosen format rather than of
    SQLite.
    """
    moments = [
        datetime(2026, 1, 9, 23, 59, 59, tzinfo=UTC),
        datetime(2026, 1, 10, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 2, 1, 6, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 30, 18, 45, 0, tzinfo=UTC),
    ]
    serialised = [to_iso(m) for m in moments]

    assert serialised == sorted(serialised)


def test_offset_form_timestamps_from_other_tools_are_accepted() -> None:
    """Other tools write "+00:00" instead of "Z"; the reader must accept both forms."""
    assert from_iso("2026-09-29T21:08:34+00:00") == from_iso("2026-09-29T21:08:34Z")


def test_a_stored_timestamp_with_no_offset_is_read_as_utc() -> None:
    """A data defect gets the least-wrong reading, and Phase 8 quarantines the row."""
    parsed = from_iso("2026-09-29T21:08:34")

    assert parsed.tzinfo is UTC
    assert parsed.hour == 21


def test_five_pm_in_chicago_becomes_the_right_utc_instant() -> None:
    """A caller saying "around five" must land in the right UTC hour.

    29 September 2026 is inside US central daylight time, UTC-5, so 17:00 local
    is 22:00 UTC. Getting this wrong by one hour is how a hold expires before the
    caller arrives.
    """
    spoken = datetime(2026, 9, 29, 17, 0)  # noqa: DTZ001 -- wall-clock input by design

    as_utc = local_wall_time_to_utc(spoken, "America/Chicago")

    assert to_iso(as_utc) == "2026-09-29T22:00:00Z"


def test_five_pm_in_chicago_in_january_is_a_different_utc_instant() -> None:
    """Outside daylight time the offset is UTC-6, which a fixed offset would miss."""
    spoken = datetime(2027, 1, 15, 17, 0)  # noqa: DTZ001 -- wall-clock input by design

    as_utc = local_wall_time_to_utc(spoken, "America/Chicago")

    assert to_iso(as_utc) == "2027-01-15T23:00:00Z"


def test_a_zone_aware_input_to_the_wall_clock_converter_is_refused() -> None:
    """Passing an already-converted instant means two conversions got confused."""
    with pytest.raises(ValueError, match="already zone-aware"):
        local_wall_time_to_utc(datetime(2026, 9, 29, 17, 0, tzinfo=UTC), "America/Chicago")


def test_staff_screens_show_local_time_not_utc() -> None:
    """Local time exists at the edges: what a caller says and what staff read."""
    instant = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)

    local = to_local(instant, "America/Chicago")

    assert (local.hour, local.minute) == (17, 0)


def test_a_future_timestamp_reports_negative_age_for_the_caller_to_handle() -> None:
    """Clock skew is real; `age_seconds` reports it rather than hiding it.

    The staleness gate clamps to zero itself, deliberately, so that skew makes
    data look *fresh* and never accidentally stale-then-refused.
    """
    future = utc_now().replace(microsecond=0)
    reference = future.replace(year=future.year - 1)

    assert age_seconds(future, now=reference) < 0
