"""Understanding what a caller said. The important behaviour is refusing to guess."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from holdline import spoken
from holdline.timeutils import local_wall_time_to_utc, to_iso, to_local, utc_now

CHICAGO = "America/Chicago"


def chicago(hour: int, minute: int = 0) -> datetime:
    """The UTC instant of a Chicago wall-clock time on a fixed reference date.

    A fixed date keeps these tests deterministic across daylight-saving boundaries:
    30 September 2026 is inside US central daylight time, so the offset is UTC-5.
    """
    naive = datetime(2026, 9, 30, hour, minute)  # noqa: DTZ001 -- wall clock by design
    return local_wall_time_to_utc(naive, CHICAGO)


# ---------------------------------------------------------------------------
# Item matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("chocolate cake", "cake_chocolate_9"),
        ("chocolate cakes", "cake_chocolate_9"),
        ("sourdough", "bread_sourdough"),
        ("almond croissant", "pastry_almond_croissant"),
        ("pain au chocolat", "pastry_pain_au_chocolat"),
        ("cheesecake", "cake_cheesecake_9"),
        ("brownie", "pastry_flourless_brownie"),
        ("focaccia", "bread_rosemary_focaccia"),
        ("cold brew", "drink_cold_brew"),
        ("latte", "drink_oat_latte"),
    ],
)
def test_a_clear_description_matches_one_item(seeded_db: Path, said: str, expected: str) -> None:
    """The ordinary case: a caller names something recognisable."""
    match = spoken.match_item(said)

    assert match.status == "matched"
    assert match.item_id == expected


def test_an_exact_item_id_short_circuits_scoring(seeded_db: Path) -> None:
    """The agent passes ids back from an earlier turn; re-scoring them could lose one."""
    match = spoken.match_item("cake_chocolate_9")

    assert match.status == "matched"
    assert match.score == 1.0


def test_the_chocolate_one_is_ambiguous_because_three_things_are_chocolate(
    seeded_db: Path,
) -> None:
    """The most important behaviour in the module is the refusal to pick.

    The catalogue holds a chocolate cake, a chocolate brownie and a pain au chocolat.
    Choosing one would be a coin flip presented as an answer.
    """
    match = spoken.match_item("the chocolate one")

    assert match.status == "ambiguous"
    assert len(match.candidates) >= 3


def test_croissant_alone_is_ambiguous_and_that_is_a_safety_property(
    seeded_db: Path,
) -> None:
    """Two croissants, and one of them has tree nuts in it.

    This is the case that shows why guessing is unacceptable rather than merely
    untidy: an almond croissant and a butter croissant are different answers to
    somebody with a nut allergy.
    """
    match = spoken.match_item("croissant")

    assert match.status == "ambiguous"
    names = {name for _, name in match.candidates}
    assert "Almond Croissant" in names
    assert "Butter Croissant" in names


def test_a_birthday_cake_is_ambiguous_across_every_cake(seeded_db: Path) -> None:
    """A category with no item named is a question, not a default."""
    match = spoken.match_item("a birthday cake")

    assert match.status == "ambiguous"
    assert len(match.candidates) >= 3


def test_something_we_do_not_sell_matches_nothing(seeded_db: Path) -> None:
    """Better to say "we don't make that" than to offer the nearest thing."""
    match = spoken.match_item("something nice")

    assert match.status == "no_match"
    assert match.item_id is None


def test_filler_words_do_not_change_the_match(seeded_db: Path) -> None:
    """People do not speak in search queries."""
    plain = spoken.match_item("sourdough")
    chatty = spoken.match_item("hi, do you still have any of the sourdough left please")

    assert chatty.status == "matched"
    assert chatty.item_id == plain.item_id


def test_an_empty_catalogue_matches_nothing_rather_than_raising(temp_db: Path) -> None:
    """An unseeded database is a configuration problem, not a crash during a call."""
    assert spoken.match_item("chocolate cake").status == "no_match"


# ---------------------------------------------------------------------------
# Pickup times
# ---------------------------------------------------------------------------


def _local_hour(moment: object) -> int:
    """The Chicago wall-clock hour of a resolved instant."""
    assert moment is not None
    return to_local(moment, CHICAGO).hour  # type: ignore[arg-type]


def test_a_bare_hour_resolves_to_the_next_time_it_comes_around(seeded_db: Path) -> None:
    """A caller saying "around five" at lunchtime means five in the afternoon.

    Resolved by picking the earliest future reading rather than by assuming pm, which
    keeps "around eight" working in the morning too.
    """
    lunchtime = chicago(13, 0)

    pickup = spoken.parse_pickup("around five", now=lunchtime)

    assert pickup.status == "parsed"
    assert _local_hour(pickup.at) == 17


def test_the_morning_reading_wins_when_it_is_still_ahead(seeded_db: Path) -> None:
    """At six in the morning, "eight" means 8am, not 8pm."""
    dawn = chicago(6, 0)

    pickup = spoken.parse_pickup("eight", now=dawn)

    assert pickup.status == "parsed"
    assert _local_hour(pickup.at) == 8


def test_an_hour_that_has_already_passed_becomes_a_question(seeded_db: Path) -> None:
    """Silently booking tomorrow would be a promise the caller never made.

    At ten at night both readings of "five" are behind us, so the assistant asks
    instead of guessing a day.
    """
    late = chicago(22, 0)

    pickup = spoken.parse_pickup("five", now=late)

    assert pickup.status == "unparsed"
    assert pickup.reason == "time_already_passed_today"


@pytest.mark.parametrize(
    ("said", "expected_hour"),
    [
        ("5:30pm", 17),
        ("5:30 pm", 17),
        ("17:30", 17),
        ("half past four", 16),
        ("quarter past six", 18),
        ("noon", 12),
        ("2pm", 14),
    ],
)
def test_named_clock_times_resolve_to_the_right_local_hour(
    seeded_db: Path,
    said: str,
    expected_hour: int,
) -> None:
    """Every form a caller uses has to land on the same clock a human would read."""
    morning = chicago(9, 0)

    pickup = spoken.parse_pickup(said, now=morning)

    assert pickup.status == "parsed", f"{said} did not parse"
    assert _local_hour(pickup.at) == expected_hour


def test_quarter_to_borrows_from_the_previous_hour(seeded_db: Path) -> None:
    """Quarter to six is 5:45, which borrows from the hour before the one said."""
    morning = chicago(9, 0)

    pickup = spoken.parse_pickup("quarter to six", now=morning)

    assert pickup.status == "parsed"
    local = to_local(pickup.at, CHICAGO)  # type: ignore[arg-type]
    assert (local.hour, local.minute) == (17, 45)


@pytest.mark.parametrize(
    ("said", "minutes"),
    [
        ("in twenty minutes", 20),
        ("in 20 minutes", 20),
        ("in ten mins", 10),
        ("in an hour", 60),
        ("in a hour", 60),
        ("in half an hour", 30),
        ("in two hours", 120),
        ("in a couple of hours", 120),
        ("in forty-five minutes", 45),
    ],
)
def test_relative_times_are_measured_from_now(seeded_db: Path, said: str, minutes: int) -> None:
    """The commonest answer of all: "I'm on my way, twenty minutes".

    The article and fraction forms matter more than they look: "in an hour" is far
    commoner than "in one hour", and a parser that only read digits would send the
    most ordinary answer a caller gives to a clarifying question.
    """
    now = utc_now()

    pickup = spoken.parse_pickup(said, now=now)

    assert pickup.status == "parsed", f"{said!r} did not parse"
    assert abs((pickup.at - now).total_seconds() - minutes * 60) < 90  # type: ignore[operator]


@pytest.mark.parametrize(
    "said",
    ["now", "asap", "right away", "as soon as possible", "on my way", "I'm coming now"],
)
def test_coming_straight_over_needs_no_clock(seeded_db: Path, said: str) -> None:
    """No specific time was named, and that is an answer rather than a failure."""
    pickup = spoken.parse_pickup(said)

    assert pickup.status == "asap"
    assert pickup.at is not None


def test_no_stated_time_at_all_is_treated_as_soon(seeded_db: Path) -> None:
    """An empty pickup time means the caller is coming shortly."""
    assert spoken.parse_pickup("").status == "asap"


def test_unreadable_text_becomes_a_question_not_a_guess(seeded_db: Path) -> None:
    """A hold against a misread time expires before the caller arrives."""
    pickup = spoken.parse_pickup("whenever suits you really")

    assert pickup.status == "unparsed"
    assert pickup.at is None


def test_tomorrow_moves_the_day_forward(seeded_db: Path) -> None:
    """Tomorrow at five is a different instant from at five, and needs approval."""
    now = utc_now()

    today = spoken.parse_pickup("at five", now=now)
    tomorrow = spoken.parse_pickup("tomorrow at five", now=now)

    assert tomorrow.status == "parsed"
    if today.status == "parsed":
        assert tomorrow.at > today.at  # type: ignore[operator]
    assert (tomorrow.at - now) > timedelta(hours=12)  # type: ignore[operator]


def test_a_resolved_pickup_is_storable_without_further_conversion(seeded_db: Path) -> None:
    """The parser's output goes straight into the ledger, so it must be UTC-aware."""
    pickup = spoken.parse_pickup("in thirty minutes")

    assert pickup.at is not None
    assert to_iso(pickup.at).endswith("Z")


def test_the_interpretation_is_reported_back_for_the_assistant_to_confirm(
    seeded_db: Path,
) -> None:
    """The assistant should be able to say what it understood, so the caller can correct it."""
    pickup = spoken.parse_pickup("in twenty minutes")

    assert pickup.interpretation is not None
    assert "20" in pickup.interpretation or "twenty" in pickup.interpretation


def test_times_are_interpreted_in_the_shops_timezone_not_the_servers(
    seeded_db: Path,
) -> None:
    """A server in another region must not shift every pickup by its own offset.

    The location row carries the timezone precisely so this cannot happen, and this is
    the test that would fail if the conversion were dropped.
    """
    # 5pm Chicago on a date inside US central daylight time is 22:00 UTC.
    morning = chicago(9, 0)

    pickup = spoken.parse_pickup("5pm", now=morning)

    assert pickup.at is not None
    assert to_iso(pickup.at) == "2026-09-30T22:00:00Z"


@pytest.mark.parametrize(
    ("said", "expected_hour"),
    [
        ("tomorrow at two", 14),
        ("tomorrow at five", 17),
        ("tomorrow at eight", 8),
        ("tomorrow at ten", 10),
        ("tomorrow at 2pm", 14),
    ],
)
def test_a_bare_hour_tomorrow_lands_inside_opening_hours(
    seeded_db: Path,
    said: str,
    expected_hour: int,
) -> None:
    """Nobody collects a cake at two in the morning.

    For today the earliest future reading is right, because the morning reading has
    usually passed and excludes itself. On a future day nothing is excluded, so
    "tomorrow at two" resolved to 2am until the shop's opening envelope was used to
    break the tie. The staff approval queue showed "Thu 2:00am", which is the kind of
    detail that makes a demo lose a room.
    """
    pickup = spoken.parse_pickup(said, now=chicago(9, 0))

    assert pickup.status == "parsed"
    assert to_local(pickup.at, CHICAGO).hour == expected_hour  # type: ignore[arg-type]
