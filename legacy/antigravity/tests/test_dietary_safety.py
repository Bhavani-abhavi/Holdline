"""The safety policy. The most consequential tests in the project.

Two properties are being proven:

1. `check_dietary_info` reports what the kitchen declared and has no way to express
   a safety conclusion, not even accidentally.
2. `assess_request` resolves every ambiguity upward, and every declared allergy
   reaches a human.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from holdline import config, dietary, holds
from holdline.exceptions import ItemNotFoundError

from helpers import BROWNIE, CAKE, LOCATION_ID, alerts_of_kind, set_stock

#: Words that would indicate the code had formed a safety opinion.
FORBIDDEN_JUDGMENT_WORDS = ("safe", "suitable", "ok_for", "okay_for", "allowed", "free_of")


def test_dietary_info_reports_what_the_kitchen_declared(seeded_db: Path) -> None:
    """The declared fields come back verbatim, with nothing added."""
    info = dietary.check_dietary_info(CAKE)

    assert info.item_name.startswith("Chocolate")
    assert "wheat" in info.contains
    assert "tree nuts" in info.may_contain
    assert "vegetarian" in info.dietary_flags


def test_dietary_info_has_no_field_in_which_a_safety_verdict_could_be_returned(
    seeded_db: Path,
) -> None:
    """The absence is the design.

    As soon as an `is_safe` field existed, something upstream would read it and say
    "yes" to a caller. This asserts that no such field exists on the dataclass or in
    the dictionary the tool layer returns -- so the judgment cannot be made here even
    by a future contributor who tries.
    """
    field_names = {f.name for f in dataclasses.fields(dietary.DietaryInfo)}
    payload_keys = set(dietary.check_dietary_info(CAKE).as_dict())

    for name in field_names | payload_keys:
        if name == "disclaimer":
            continue  # the disclaimer's text says what must NOT be claimed
        assert not any(word in name.lower() for word in FORBIDDEN_JUDGMENT_WORDS), (
            f"{name!r} looks like a safety verdict"
        )


def test_the_ingredient_lookup_carries_its_own_disclaimer(seeded_db: Path) -> None:
    """The text placed in the model's context already states the boundary.

    The guardrail in Phase 3 enforces it; this makes the rule visible at the point
    where the ingredients are read, rather than only in a system prompt.
    """
    info = dietary.check_dietary_info(BROWNIE)

    assert "not a safety assessment" in info.disclaimer.lower()
    assert info.as_dict()["disclaimer"] == info.disclaimer


def test_an_unknown_item_raises_rather_than_reporting_no_ingredients(seeded_db: Path) -> None:
    """An empty ingredient list reads as "contains nothing", which is the worst answer.

    For an allergen question, silently returning `contains: []` for an item nobody
    recognised would be the single most dangerous behaviour in the system.
    """
    with pytest.raises(ItemNotFoundError):
        dietary.check_dietary_info("cake_that_does_not_exist")


def test_the_gluten_free_item_still_declares_its_wheat_cross_contact(seeded_db: Path) -> None:
    """The case that makes inference unsafe, proven end to end through the tool.

    An item flagged `gluten_free` whose `may_contain` includes wheat is not bad data
    -- it is what a shared bench means. Anything reasoning from the marketing flag to
    "safe for coeliac disease" would be confidently wrong.
    """
    info = dietary.check_dietary_info(BROWNIE)

    assert "gluten_free" in info.dietary_flags
    assert "wheat" in info.may_contain


@pytest.mark.parametrize(
    "stated",
    [
        "my daughter's allergic to peanuts",
        "I have a peanut allergy",
        "she's coeliac",
        "he's celiac",
        "lactose intolerant",
        "I carry an epipen",
        "anaphylaxis to shellfish",
        "she can't have dairy",
        "he cannot eat gluten",
        "nut free please",
        "no nuts",
        "is this gluten free?",
        "my son can't have gluten, will this work?",
        "it makes my daughter sick",
        "she had a reaction last time",
        "not allowed eggs",
        "needs to avoid sesame",
    ],
)
def test_anything_resembling_an_allergy_is_classified_as_one(stated: str) -> None:
    """Deliberately over-inclusive. A false positive costs one phone call.

    Several of these are not medically allergies at all -- "is this gluten free?"
    might be a preference, "no nuts" might be a dislike. They escalate anyway,
    because the cost of being wrong is not symmetric and this classifier has no
    business guessing which is which.
    """
    assessment = dietary.assess_request(stated)

    assert assessment.severity == "allergy", f"{stated!r} was not escalated"
    assert assessment.requires_escalation is True


@pytest.mark.parametrize(
    "stated",
    [
        "",
        "   ",
        "no",
        "none",
        "nope",
        "no thanks",
        "nothing",
        "no allergies",
        "no known allergies",
        "no allergies at all",
        "no nut allergies",
        "not allergic",
        "no dietary restrictions",
        "none that I know of",
        "all good",
        "we're fine",
        "not really",
    ],
)
def test_a_caller_who_says_no_is_not_escalated(stated: str) -> None:
    """The false positive that would otherwise escalate every single call.

    "No allergies" contains the substring "allerg". A naive substring search would
    classify the most common answer to the assistant's own question as an allergy,
    escalate every caller, and make the feature useless. Denials are matched against
    the *whole* answer for exactly this reason.
    """
    assessment = dietary.assess_request(stated)

    assert assessment.severity == "none", f"{stated!r} was wrongly escalated"
    assert assessment.requires_escalation is False


@pytest.mark.parametrize(
    "stated",
    [
        "no nut allergies but she can't have dairy",
        "no allergies, though I'd avoid sesame",
        "none for me, my wife is coeliac",
    ],
)
def test_a_denial_followed_by_a_real_constraint_still_escalates(stated: str) -> None:
    """A denial only counts when it is the entire answer.

    This is the subtlest safety detail in the module. "No nut allergies" is a
    denial; "no nut allergies but she can't have dairy" is a declaration. A
    substring match would treat them identically and miss the dairy.
    """
    assert dietary.assess_request(stated).severity == "allergy"


@pytest.mark.parametrize(
    "stated", ["vegan please", "we're vegetarian", "kosher", "keto if possible"]
)
def test_a_choice_is_recorded_as_a_preference_not_an_allergy(stated: str) -> None:
    """Preferences reach the kitchen without raising an urgent callback."""
    assessment = dietary.assess_request(stated)

    assert assessment.severity == "preference"
    assert assessment.requires_escalation is False


def test_an_unparseable_answer_to_the_allergy_question_becomes_a_preference() -> None:
    """The caller answered the question, so the kitchen hears about it either way.

    Classifying unparsed text as "none" would throw away something a caller
    deliberately said. "Preference" keeps it, without claiming it is medical. This
    only applies to a direct answer -- see the companion test below.
    """
    assessment = dietary.assess_request("she really loves the pink ones", direct_answer=True)

    assert assessment.severity == "preference"
    assert "unclassified" in assessment.matched_terms


def test_allergy_escalation_is_not_configurable() -> None:
    """The one rule with no off switch."""
    assert config.ALLERGY_ALWAYS_ESCALATE is True


def test_a_hold_with_an_allergy_note_is_never_activated_automatically(seeded_db: Path) -> None:
    """The policy, enforced in the ledger rather than requested in a prompt.

    Plenty of stock, small quantity, imminent pickup -- everything about this hold
    would otherwise auto-approve. The allergy alone routes it to a human.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    result = holds.create_hold(
        LOCATION_ID, CAKE, 1, dietary_note="my daughter is allergic to peanuts"
    )

    assert result.ok is True
    assert result.status == "pending_approval"
    assert "dietary_note_present" in result.approval_reasons
    assert result.dietary_severity == "allergy"


def test_a_hold_with_an_allergy_raises_an_urgent_callback_alert(seeded_db: Path) -> None:
    """Staff are interrupted, not merely informed.

    The callback alert is raised *in addition to* the approval alert rather than
    instead of it, so acknowledging the routine one cannot make the urgent one
    disappear from the queue.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    holds.create_hold(
        LOCATION_ID,
        CAKE,
        1,
        customer_name="Priya",
        customer_phone="+13125550188",
        dietary_note="peanut allergy",
    )

    callbacks = alerts_of_kind("allergy_callback")
    approvals = alerts_of_kind("approval_needed")

    assert len(callbacks) == 1
    assert callbacks[0]["urgency"] == "urgent"
    assert len(approvals) == 1


def test_a_preference_still_needs_a_human_but_raises_no_urgent_alert(seeded_db: Path) -> None:
    """Any dietary note forces approval; only an allergy sounds the alarm.

    A preference reaching the kitchen through an automated line deserves a person's
    eyes, but treating "vegan please" as urgent would train staff to ignore urgency.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    result = holds.create_hold(LOCATION_ID, CAKE, 1, dietary_note="vegan if possible")

    assert result.status == "pending_approval"
    assert result.dietary_severity == "preference"
    assert alerts_of_kind("allergy_callback") == []


@pytest.mark.parametrize(
    "stated",
    [
        "and no allergies",
        "Yes please, hold one for me. I'm Dana, and no allergies.",
        "so no allergies",
        "oh, and no allergies",
        "yeah no allergies",
        "and um, no allergies",
        "just no allergies thanks",
        "Twenty minutes, and none for us.",
    ],
)
def test_a_denial_introduced_by_a_conjunction_is_still_a_denial(stated: str) -> None:
    """Real callers say "and no allergies", not "no allergies".

    The denial has to match its whole clause, and with a leading "and" attached it does
    not -- so it fell through to the medical markers and escalated. This was found by a
    live call, not by the suite: every scripted test had used the bare phrasing. See
    DEFECTS.md entry 8.
    """
    assert dietary.assess_request(stated).severity == "none", f"{stated!r} escalated"


@pytest.mark.parametrize(
    "stated",
    [
        "and she's allergic to peanuts",
        "oh, and no dairy for my son, he's intolerant",
        "so my daughter can't have gluten",
    ],
)
def test_stripping_a_conjunction_does_not_swallow_a_real_declaration(stated: str) -> None:
    """The filter removes conjunctions, never meaning.

    A leading "no" or "not" is meaning and is never stripped, so the fix cannot turn a
    declaration into a denial -- which would be the dangerous direction of this change.
    """
    assert dietary.assess_request(stated).severity == "allergy"


@pytest.mark.parametrize(
    ("stated", "collides_with"),
    [
        ("I'll be there in twenty minutes", "nut inside mi-nut-es"),
        ("give me a minute", "nut inside mi-nut-e"),
        ("we're just round the corner", "corn inside corn-er"),
        ("in about ten minutes", "nut inside mi-nut-es"),
        ("I'll be a minute or two", "nut inside mi-nut-e"),
    ],
)
def test_an_allergen_name_inside_an_ordinary_word_is_not_a_declaration(
    stated: str,
    collides_with: str,
) -> None:
    """Allergen names are matched as words, not as substrings.

    This is the most consequential false positive found in the project, and it was found
    by a live call rather than by the suite. "Twenty minutes" is in a large share of
    pickup conversations, and every one of them escalated as a nut allergy: a manager
    paged, an urgent callback raised, the hold parked for approval. See DEFECTS.md
    entry 9.
    """
    assert dietary.assess_request(stated).severity == "none", (
        f"{stated!r} escalated on {collides_with}"
    )


def test_the_real_phrase_a_live_caller_used_does_not_escalate() -> None:
    """The exact sentence from the live transcript that exposed both bugs at once.

    It contained "minutes" (nut) and "and no allergies" (a denial the clause matcher did
    not recognise with the conjunction attached). Either one alone was enough to route a
    caller who explicitly said they had no allergies to a human.
    """
    stated = (
        "Yes please, hold one for me. I'm Dana, I'll be there in about twenty "
        "minutes, and no allergies."
    )

    assert dietary.assess_request(stated).severity == "none"


def test_plural_and_singular_allergen_names_both_match() -> None:
    """Word boundaries must not cost the plural forms callers actually use."""
    for stated in ("no nuts", "no nut", "peanuts please no", "avoid eggs", "avoid egg"):
        assert dietary.assess_request(stated).severity == "allergy", stated
