"""The guardrail. Both tiers, and every false positive that would get it switched off.

A validator that blocks legitimate sentences is a validator somebody disables, so
the false-positive cases carry as much weight here as the violations. Each one is
its own named test, because a failure should read as a sentence about what broke.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from holdline import config, guardrails, inventory
from holdline import tools as real_tools
from holdline.guardrails import ConversationState, resolve, validate_response
from holdline.tools import ToolResult

from helpers import alerts_of_kind, events_of_kind

CAKE_NAME = 'Chocolate Fudge Cake, 9"'


# ---------------------------------------------------------------------------
# Builders for this turn's tool results
# ---------------------------------------------------------------------------


def availability(quantity: int = 11, *, item_name: str = CAKE_NAME) -> ToolResult:
    """A check_availability result that vouched for a number."""
    return ToolResult(
        name="check_availability",
        arguments={"item_query": "chocolate cake"},
        ok=True,
        payload={
            "item_id": "cake_chocolate_9",
            "status": "known",
            "item_name": item_name,
            "quantity": quantity,
            "as_of": "2026-09-30T14:03:11+00:00",
        },
    )


def stale_availability() -> ToolResult:
    """A check_availability result that refused to state a number."""
    return ToolResult(
        name="check_availability",
        arguments={"item_query": "chocolate cake"},
        ok=True,
        payload={
            "item_id": "cake_chocolate_9",
            "status": "unknown",
            "item_name": CAKE_NAME,
            "reason": "stale_data",
            "age_seconds": 1800,
        },
    )


def hold(*, status: str = "active", hold_id: int = 7, quantity: int = 1) -> ToolResult:
    """A successful create_hold result."""
    return ToolResult(
        name="create_hold",
        arguments={"item_id": "cake_chocolate_9", "quantity": quantity},
        ok=True,
        payload={
            "ok": True,
            "hold_id": hold_id,
            "status": status,
            "item_name": CAKE_NAME,
            "quantity": quantity,
            "needs_approval": status == "pending_approval",
        },
    )


def failed_hold(reason: str = "insufficient") -> ToolResult:
    """A create_hold result that refused."""
    return ToolResult(
        name="create_hold",
        arguments={"item_id": "cake_chocolate_9", "quantity": 1},
        ok=False,
        payload={"ok": False, "reason": reason, "item_name": CAKE_NAME},
    )


def dietary(
    contains: list[str] | None = None,
    may_contain: list[str] | None = None,
) -> ToolResult:
    """A check_dietary_info result carrying the kitchen's declaration."""
    return ToolResult(
        name="check_dietary_info",
        arguments={"item_id": "cake_chocolate_9"},
        ok=True,
        payload={
            "item_id": "cake_chocolate_9",
            "item_name": CAKE_NAME,
            "contains": contains if contains is not None else ["wheat", "eggs", "dairy"],
            "may_contain": may_contain if may_contain is not None else ["tree nuts", "peanuts"],
            "dietary_flags": ["vegetarian"],
            "disclaimer": "This is not a safety assessment.",
        },
    )


def state(**overrides: Any) -> ConversationState:
    """A conversation state with sensible defaults."""
    base = ConversationState(call_id="call-test", channel="browser")
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def check(
    text: str,
    results: list[ToolResult] | None = None,
    **state_kwargs: Any,
) -> guardrails.ValidationResult:
    """Validate one response. Returns the ValidationResult."""
    return validate_response(text, results or [], state(**state_kwargs))


def rules_of(result: guardrails.ValidationResult) -> set[str]:
    """The set of rule names that fired."""
    return {v.rule for v in result.violations}


# ---------------------------------------------------------------------------
# TIER 1 -- quantity grounding
# ---------------------------------------------------------------------------


def test_a_quantity_the_tool_returned_is_allowed() -> None:
    """The ordinary case: the assistant repeats what it looked up."""
    result = check("We do, eleven chocolate cakes right now.", [availability(11)])

    assert result.ok is True
    assert result.tier == 0


def test_a_quantity_no_tool_returned_is_blocked() -> None:
    """The headline failure mode. Nothing in this turn supports "twenty"."""
    result = check("We've got twenty chocolate cakes.", [availability(11)])

    assert result.ok is False
    assert result.tier == 1
    assert "quantity_grounding" in rules_of(result)


def test_a_number_word_is_caught_as_well_as_a_numeral() -> None:
    """Writing it in words must not be an escape hatch."""
    digits = check("We have 20 left.", [availability(11)])
    words = check("We have twenty left.", [availability(11)])

    assert digits.ok is False
    assert words.ok is False


def test_a_dozen_is_read_as_twelve(seeded_db: Path) -> None:
    """Idiomatic quantities count. "A dozen" claims twelve, and twelve is not eleven."""
    result = check("We've got a dozen chocolate cakes.", [availability(11)])

    assert result.ok is False
    assert "quantity_grounding" in rules_of(result)


def test_half_a_dozen_is_six_and_not_twelve() -> None:
    """Longest-phrase-first matching, or "half a dozen" silently becomes twelve."""
    mentions, _ = guardrails.extract_quantities("I can do half a dozen.")

    assert [m.value for m in mentions] == [6]


def test_a_couple_is_read_as_two() -> None:
    """The specification names this one explicitly."""
    result = check("We have a couple of chocolate cakes.", [availability(11)])

    assert result.ok is False
    assert any("2" in v.detail for v in result.violations)


def test_a_compound_number_is_read_as_one_value() -> None:
    """Spoken as two words, twenty five is 25 rather than 20 and 5 separately."""
    mentions, _ = guardrails.extract_quantities("We baked twenty five this morning.")

    assert [m.value for m in mentions] == [25]


def test_an_offer_to_reserve_within_what_is_available_is_allowed() -> None:
    """Offering two of eleven is honest, and create_hold settles whether it happens.

    Without the proposal path the assistant could not offer to do anything: every
    offer would name a number no tool had returned.
    """
    result = check("Want me to set two aside for you?", [availability(11)])

    assert result.ok is True


def test_an_offer_beyond_what_is_available_is_blocked() -> None:
    """A proposal is still bounded by what the last availability check found."""
    result = check("Want me to set twenty aside for you?", [availability(11)])

    assert result.ok is False
    assert "quantity_grounding" in rules_of(result)


def test_the_readme_transcript_passes_exactly_as_written() -> None:
    """The documented demo must survive its own guardrail.

    An assertion of eleven, a masked nine-inch size, and a proposal of one. If this
    ever fails, either the README is a lie or the validator is broken.
    """
    result = check(
        "We do, eleven nine-inch chocolate cakes right now. They tend to go by early "
        "evening. Want me to set one aside?",
        [availability(11)],
    )

    assert result.ok is True


def test_a_quantity_the_customer_stated_is_allowed() -> None:
    """Echoing the caller's own number back is not the assistant inventing one."""
    conversation = state()
    conversation.note_customer_turn("Can I get three croissants?")

    result = validate_response("Three croissants, got it.", [], conversation)

    assert result.ok is True


def test_vague_quantifiers_are_not_quantity_claims() -> None:
    """A vague quantifier asserts no number, so there is no number to check.

    Deliberate: banning vagueness would remove the assistant's only honest way to
    talk about stock it cannot count.
    """
    result = check("We have a few left, and plenty of croissants.", [availability(11)])

    assert result.ok is True


# ---------------------------------------------------------------------------
# False positives -- each its own case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("clock time with meridiem", "That's set for 5:45pm."),
        ("twenty-four hour time", "We'll have it ready at 17:30."),
        ("bare meridiem hour", "Pickup at 5pm works."),
        ("o'clock in words", "Come by at five o'clock."),
        ("price with symbol", "That comes to $24."),
        ("price with cents", "The cake is $42.00."),
        ("price in words", "It's 42 dollars."),
        ("phone number", "I'll call you on 312-555-0188."),
        ("numeric date", "We could do 10/14 instead."),
        ("written date", "That would be October 14th."),
        ("hold reference", "Your hold number is 7."),
        ("hash reference", "That's order #7."),
        ("street address", "We're at 2214 N Halsted St."),
        ("postal code", "That's Chicago 60614."),
        ("item size in words", "It's a nine-inch cake."),
        ("item size in digits", "It's a 9-inch cake."),
        ("drink volume", "The cold brew is 16oz."),
        ("ordinal word", "The first batch comes out soon."),
        ("numeric ordinal", "We're closed on the 21st."),
        ("pronoun one", "The chocolate one is my favourite."),
        ("one as a moment", "One moment while I check."),
        ("a year", "We've been here since 2019."),
        ("percentage", "It's 20% off today."),
    ],
)
def test_non_quantity_numbers_are_not_treated_as_quantity_claims(label: str, text: str) -> None:
    """Times, prices, phone numbers, dates, ids, addresses, sizes and ordinals.

    Every one of these contains a number that no tool returned. Treating any of them
    as an ungrounded quantity claim would make the assistant unable to say ordinary
    things, which is how a guardrail ends up switched off.
    """
    result = check(text, [availability(11)])

    assert result.ok is True, f"{label} was wrongly flagged: {result.violations}"


def test_a_sentence_mixing_a_price_a_time_and_a_grounded_quantity_passes() -> None:
    """The realistic case is all of them at once, in one breath."""
    result = check(
        "We have eleven at $42.00 each, and I can hold one until 5:45pm.",
        [availability(11)],
    )

    assert result.ok is True


def test_masking_a_price_does_not_merge_two_sentences() -> None:
    """A price contains a full stop, so masking it removes a sentence boundary.

    Splitting the masked text instead of masking each sentence would misalign
    sentences with their originals, and every proposal/assertion decision after the
    first price would be made against the wrong sentence.
    """
    mentions, _ = guardrails.extract_quantities("It costs $24.00. Want me to hold two?")

    assert [m.value for m in mentions] == [2]
    assert mentions[0].is_proposal is True


def test_the_reported_span_points_at_the_offending_sentence() -> None:
    """A violation nobody can locate is a violation nobody can fix."""
    result = check("Hours are 7am to 6pm. We have thirty cakes.", [availability(11)])

    assert result.ok is False
    assert "thirty" in result.violations[0].offending_text
    assert "Hours" not in result.violations[0].offending_text


def test_what_was_masked_is_reported_for_diagnosis() -> None:
    """A false positive should be diagnosable from a log line, not by re-reading regexes."""
    result = check("Ready at 5:45pm for $42.00.", [availability(11)])

    assert {name for name, _ in result.masked_spans} >= {"time_colon", "price"}


# ---------------------------------------------------------------------------
# TIER 1 -- hold confirmation
# ---------------------------------------------------------------------------


def test_confirming_a_hold_that_exists_is_allowed() -> None:
    """The ordinary success path."""
    result = check("You're all set, I've put one aside for you.", [availability(11), hold()])

    assert result.ok is True


def test_confirming_a_hold_that_was_never_created_is_blocked() -> None:
    """The promise the ledger never made."""
    result = check("You're all set, it's reserved under your name.", [availability(11)])

    assert result.ok is False
    assert result.tier == 1
    assert "hold_confirmation" in rules_of(result)


def test_confirming_a_hold_the_ledger_refused_is_blocked() -> None:
    """A refused create_hold is not a hold, however the reply is phrased."""
    result = check("That's set aside for you.", [failed_hold()])

    assert result.ok is False
    assert "hold_confirmation" in rules_of(result)


def test_offering_to_reserve_is_not_confirming_a_reservation() -> None:
    """The distinction that lets the assistant ask before it acts."""
    result = check("Want me to set one aside for you?", [availability(11)])

    assert result.ok is True


# ---------------------------------------------------------------------------
# TIER 1 -- stale override
# ---------------------------------------------------------------------------


def test_no_quantity_may_be_stated_when_availability_is_unknown() -> None:
    """Stale data removes the ability to promise, not merely the confidence."""
    result = check("I think we have about eleven.", [stale_availability()])

    assert result.ok is False
    assert "stale_override" in rules_of(result)


def test_even_an_offer_naming_a_number_is_blocked_under_stale_data() -> None:
    """Stricter than rule 1 on purpose: create_hold is about to refuse anyway."""
    result = check("Want me to set two aside?", [stale_availability()])

    assert result.ok is False
    assert "stale_override" in rules_of(result)


def test_saying_so_without_a_number_is_allowed_under_stale_data() -> None:
    """The correct behaviour has to remain available, or the rule is unusable."""
    result = check(
        "I can't see a current count on the chocolate cake, so let me check with "
        "someone and call you back.",
        [stale_availability()],
    )

    assert result.ok is True


def test_a_number_the_customer_said_survives_the_stale_rule() -> None:
    """Repeating the caller's own request is not quoting stock."""
    conversation = state()
    conversation.note_customer_turn("I wanted two chocolate cakes.")

    result = validate_response("Two chocolate cakes.", [stale_availability()], conversation)

    assert result.ok is True


# ---------------------------------------------------------------------------
# TIER 2 -- allergen claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("hyphenated free-from", "It's peanut-free, so you're fine."),
        ("spaced free-from", "That one is nut free."),
        ("free of", "It's free of tree nuts."),
        ("free from", "This is free from dairy."),
        ("safe for a person", "That's safe for her."),
        ("safe for an allergy", "It's safe for a peanut allergy."),
        ("bare safety assertion", "It's completely safe."),
        ("suitable for", "That's suitable for your daughter."),
        ("does not contain", "It doesn't contain any peanuts."),
        ("will not contain", "It won't contain nuts."),
        ("no allergen in it", "There's no gluten in it."),
        ("without any", "It's made without any dairy."),
    ],
)
def test_asserting_an_item_is_free_of_something_is_a_tier_two_block(
    label: str,
    text: str,
) -> None:
    """The rule in one line: the assistant may state presence, never absence.

    Blocked whether or not a dietary tool ran, and whether or not the claim is true. A
    caller on a phone hears "gluten-free" and does not hear the attribution.
    """
    result = check(text, [availability(11), dietary()])

    assert result.ok is False, f"{label} was not blocked"
    assert result.tier == 2


def test_a_true_absence_claim_is_still_blocked() -> None:
    """Correctness is not the test. Authority is.

    The kitchen's declaration says no peanuts here, and the assistant still may not be
    the thing that tells a caller so -- the catalogue's gluten-free brownie also
    declares wheat cross-contact, so the flags do not mean what a caller hears.
    """
    result = check(
        "Good news, that cake is peanut-free.",
        [dietary(contains=["wheat"], may_contain=[])],
    )

    assert result.ok is False
    assert result.tier == 2


def test_reassurance_is_blocked_once_a_dietary_need_has_been_declared() -> None:
    """Casual reassurance is a safety judgment wearing friendly clothes."""
    result = check(
        "She'll be fine with that one.",
        [availability(11)],
        dietary_severity="allergy",
        declared_dietary="allergic to peanuts",
    )

    assert result.ok is False
    assert result.tier == 2
    assert "allergen_reassurance" in rules_of(result)


def test_the_same_reassurance_in_an_ordinary_booking_is_allowed() -> None:
    """The false positive that would make the guardrail unusable.

    "That's fine" is one of the most common things a person says on a phone. Blocking
    it whenever the word appears would be the end of the assistant sounding human.
    """
    result = check("That's fine, five o'clock works.", [availability(11)])

    assert result.ok is True


def test_suitable_for_an_occasion_is_not_a_safety_claim() -> None:
    """Suitability for an occasion says nothing about anybody's health."""
    result = check("That one's suitable for a birthday party.", [availability(11)])

    assert result.ok is True


def test_saying_an_item_lacks_a_topping_is_not_an_allergen_claim() -> None:
    """The absence pattern needs an allergen named, not merely a negation."""
    result = check("It doesn't have a cherry on top.", [availability(11)])

    assert result.ok is True


def test_relaying_what_an_item_contains_is_allowed_after_a_lookup() -> None:
    """Presence is the half the assistant is allowed to state."""
    result = check(
        "That one contains wheat, eggs and dairy, and it's made on shared equipment "
        "with tree nuts.",
        [dietary()],
    )

    assert result.ok is True


def test_a_common_word_for_a_declared_allergen_is_accepted() -> None:
    """Relaying "walnuts" against a declared "tree nuts" must not read as invention.

    Callers do not speak in allergen taxonomy, and a validator that demanded it would
    fire on the single most important thing the assistant does correctly.
    """
    result = check(
        "It has walnuts in the batter.",
        [dietary(contains=["wheat", "tree nuts"], may_contain=[])],
    )

    assert result.ok is True


def test_describing_ingredients_without_looking_them_up_is_a_tier_one_failure() -> None:
    """Over-warning is the safe direction of error, so this one gets its retry."""
    result = check("That cake contains peanuts.", [availability(11)])

    assert result.ok is False
    assert result.tier == 1
    assert "ungrounded_allergen_presence" in rules_of(result)


def test_naming_an_allergen_the_kitchen_did_not_declare_is_a_tier_one_failure() -> None:
    """Inventing an ingredient costs a sale, not a life -- but it is still invented."""
    result = check(
        "That one contains shellfish.",
        [dietary(contains=["wheat", "eggs"], may_contain=[])],
    )

    assert result.ok is False
    assert result.tier == 1
    assert "ungrounded_allergen_presence" in rules_of(result)


# ---------------------------------------------------------------------------
# TIER 2 -- autonomous allergy commitment
# ---------------------------------------------------------------------------


def test_a_hold_awaiting_approval_may_be_confirmed_with_an_allergy_declared() -> None:
    """The correct behaviour: a soft hold, and a person to confirm it."""
    result = check(
        "I'm putting a soft hold on the cake and having someone call you back.",
        [availability(11), hold(status="pending_approval")],
        dietary_severity="allergy",
        declared_dietary="allergic to peanuts",
    )

    assert result.ok is True


def test_confirming_an_active_hold_with_an_allergy_declared_is_blocked() -> None:
    """The automated line may not settle an allergy question by itself."""
    result = check(
        "You're all set, it's reserved for you.",
        [availability(11), hold(status="active")],
        dietary_severity="allergy",
        declared_dietary="allergic to peanuts",
    )

    assert result.ok is False
    assert result.tier == 2
    assert "autonomous_allergy_commitment" in rules_of(result)


def test_confirming_no_hold_at_all_with_an_allergy_declared_is_blocked() -> None:
    """Worse than the ungrounded case, and tiered accordingly."""
    result = check(
        "That's all set aside for you.",
        [availability(11)],
        dietary_severity="allergy",
        declared_dietary="allergic to peanuts",
    )

    assert result.ok is False
    assert result.tier == 2


def test_confirming_an_active_hold_without_an_allergy_is_fine() -> None:
    """The rule is scoped to allergies, and must not tax ordinary orders."""
    result = check("You're all set, I've put one aside.", [availability(11), hold()])

    assert result.ok is True


# ---------------------------------------------------------------------------
# Tiering and recovery
# ---------------------------------------------------------------------------


def test_a_first_grounding_failure_earns_one_retry(seeded_db: Path) -> None:
    """Tier 1 is recoverable, once."""
    conversation = state()
    result = validate_response("We have thirty cakes.", [availability(11)], conversation)

    action = resolve(result, conversation, "We have thirty cakes.")

    assert action.action == "retry"
    assert action.correction is not None
    assert "thirty" in action.correction or "30" in action.correction
    assert conversation.guardrail_retries == 1


def test_a_second_grounding_failure_falls_back_to_something_safe(seeded_db: Path) -> None:
    """One retry, then stop. A second pass that happened to work proves nothing."""
    conversation = state(guardrail_retries=config.GUARDRAIL_MAX_RETRIES)
    result = validate_response("We have thirty cakes.", [availability(11)], conversation)

    action = resolve(result, conversation, "We have thirty cakes.")

    assert action.action == "safe_fallback"
    assert action.text == guardrails.SAFE_FALLBACK


def test_the_safe_fallback_contains_no_number_of_any_kind() -> None:
    """A fallback that quoted a number would defeat its own purpose."""
    assert not any(character.isdigit() for character in guardrails.SAFE_FALLBACK)


def test_a_safety_failure_is_never_retried(seeded_db: Path) -> None:
    """The centrepiece of the tiering.

    No retry even on the first occurrence and even with the retry budget untouched. A
    regenerated answer that happens to pass validation is still a model guessing about
    anaphylaxis, and randomness is not a safety mechanism.
    """
    conversation = state(guardrail_retries=0)
    text = "It's peanut-free, she'll be fine."
    result = validate_response(text, [dietary()], conversation)

    action = resolve(result, conversation, text)

    assert result.tier == 2
    assert action.action == "safety_block"
    assert action.text == guardrails.SAFETY_ESCALATION
    assert conversation.guardrail_retries == 0
    assert conversation.escalated is True


def test_a_safety_failure_still_blocks_after_a_tier_one_retry(seeded_db: Path) -> None:
    """Spending the retry budget on grounding must not open a path for tier 2."""
    conversation = state(guardrail_retries=config.GUARDRAIL_MAX_RETRIES)
    text = "It's nut free."
    result = validate_response(text, [dietary()], conversation)

    action = resolve(result, conversation, text)

    assert action.action == "safety_block"


def test_a_safety_failure_raises_an_urgent_alert_for_staff(seeded_db: Path) -> None:
    """Somebody has to call this caller back, and they have to know now."""
    conversation = state()
    text = "That's safe for her."
    resolve(validate_response(text, [dietary()], conversation), conversation, text)

    alerts = alerts_of_kind("guardrail_tier2")
    assert len(alerts) == 1
    assert alerts[0]["urgency"] == "urgent"


def test_every_violation_is_recorded_with_its_tier_and_the_offending_text(
    seeded_db: Path,
) -> None:
    """Phase 7 counts these, and Phase 8 trends them. Unrecorded means unmeasurable."""
    conversation = state()
    text = "We have thirty cakes."
    resolve(validate_response(text, [availability(11)], conversation), conversation, text)

    recorded = events_of_kind("guardrail_violation")
    assert len(recorded) == 1
    detail = str(recorded[0]["detail"])
    assert '"tier": 1' in detail
    assert "thirty" in detail


def test_a_passing_response_is_accepted_unchanged(seeded_db: Path) -> None:
    """The common path must not rewrite anything."""
    conversation = state()
    text = "We have eleven right now."
    action = resolve(validate_response(text, [availability(11)], conversation), conversation, text)

    assert action.action == "accept"
    assert action.text == text


def test_all_violations_are_reported_not_just_the_first() -> None:
    """The corrective message can only name what the validator collected."""
    result = check("We have thirty cakes and it's all set aside.", [availability(11)])

    assert rules_of(result) >= {"quantity_grounding", "hold_confirmation"}


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------


def test_the_state_collects_numbers_the_caller_said() -> None:
    """Those numbers are part of the grounded set for the rest of the turn."""
    conversation = state()
    conversation.note_customer_turn("Can I get three, maybe four?")

    assert {3, 4} <= conversation.customer_numbers


def test_the_state_notices_an_allergy_and_never_forgets_it() -> None:
    """Severity only escalates. A caller who mentioned an allergy still mentioned one."""
    conversation = state()
    conversation.note_customer_turn("My daughter is allergic to peanuts.")
    assert conversation.dietary_severity == "allergy"

    conversation.note_customer_turn("Anyway, lovely weather today.")

    assert conversation.dietary_severity == "allergy"
    assert conversation.declared_dietary is not None


def test_a_preference_does_not_become_an_allergy() -> None:
    """The two tiers of dietary need stay distinct."""
    conversation = state()
    conversation.note_customer_turn("Vegan if possible.")

    assert conversation.dietary_severity == "preference"


# ---------------------------------------------------------------------------
# The contract between tools.py and guardrails.py
# ---------------------------------------------------------------------------


def test_the_guardrail_reads_real_tool_output_not_just_the_fixtures(seeded_db: Path) -> None:
    """The fixtures above are hand-built, so something must check they match reality.

    Without this, every guardrail test could pass against payload shapes the real
    tools never produce -- the validator would be verified against a fiction, and the
    first live call would be the first real test. This drives the actual tools and
    asserts the guardrail's readers find what they need in what comes back.
    """
    availability_result = real_tools.dispatch(
        "check_availability", {"item_query": "chocolate cake"}
    )
    hold_result = real_tools.dispatch(
        "create_hold",
        {"item_id": "chocolate cake", "quantity": 1, "pickup_time": "in 30 minutes"},
    )
    dietary_result = real_tools.dispatch("check_dietary_info", {"item_id": "chocolate cake"})
    results = [availability_result, hold_result, dietary_result]

    # The three readers the rules depend on must all find their subject.
    assert guardrails._last_known_quantity(results) == 11
    assert guardrails._successful_holds(results)
    assert guardrails._dietary_lookups(results)
    assert guardrails._any_availability_unknown(results) is False


def test_a_real_stale_lookup_triggers_the_stale_rule(seeded_db: Path) -> None:
    """The staleness gate and the guardrail have to agree on what "unknown" looks like."""
    inventory.force_stale(config.DEFAULT_LOCATION_ID)
    stale = real_tools.dispatch("check_availability", {"item_query": "chocolate cake"})

    assert guardrails._any_availability_unknown([stale]) is True
    assert check("We have eleven left.", [stale]).ok is False


def test_the_readme_transcript_passes_against_real_tool_output(seeded_db: Path) -> None:
    """The documented demo, validated end to end against the real availability tool."""
    lookup = real_tools.dispatch("check_availability", {"item_query": "chocolate cake"})

    result = check(
        "We do, eleven nine-inch chocolate cakes right now. Want me to set one aside?",
        [lookup],
    )

    assert result.ok is True


@pytest.mark.parametrize(
    "text",
    [
        "We've got eleven right now, though it can shift if someone buys one at the counter.",
        "That's eleven, but somebody could take one before you arrive.",
        "Eleven at the moment. People do grab one on the way home.",
        "Eleven now, and they sometimes buy one late in the day.",
    ],
)
def test_a_third_party_buying_one_is_a_hedge_not_a_stock_claim(text: str) -> None:
    """The honest caveat a good cashier adds, and it was being blocked.

    A live model answered "are you sure?" with exactly this shape -- eleven, plus a
    caveat that someone might buy one at the counter -- and the pronoun "one" was read as
    an ungrounded quantity claim. The assistant was punished for being careful.

    The subject list is third-party only. "We have one left" is a real claim about stock
    and must still reach the grounding check, which the next test holds.
    """
    assert check(text, [availability(11)]).ok is True, f"blocked: {text!r}"


def test_the_shop_claiming_to_have_one_is_still_checked() -> None:
    """The narrowness of the previous fix, asserted from the other side.

    If masking "one" after a third-party subject had been written loosely enough to
    cover "we have one", it would have opened a hole in the main rule.
    """
    assert check("We have one left.", [availability(11)]).ok is False
    assert check("There's only one left.", [availability(11)]).ok is False


def test_offering_to_hold_one_works_when_the_item_is_still_ambiguous() -> None:
    """The ambiguity path has to be able to ask its own question.

    `check_availability` returns candidates and no quantity, so there is no ceiling for a
    proposal to be measured against. Blocking "which one would you like me to hold?"
    would mean the assistant could never ask the question the ambiguous result exists to
    prompt. A live call hit this on "can you put a croissant aside for me?".
    """
    ambiguous = ToolResult(
        name="check_availability",
        arguments={"item_query": "croissant"},
        ok=True,
        payload={
            "status": "ambiguous",
            "item_query": "croissant",
            "candidates": ["Almond Croissant", "Butter Croissant"],
        },
    )

    result = check("We've got almond and butter. Shall I hold one for you?", [ambiguous])

    assert result.ok is True


def test_offering_more_than_one_still_needs_a_ceiling() -> None:
    """The exemption is for a single unit only, and not a general licence."""
    ambiguous = ToolResult(
        name="check_availability",
        arguments={"item_query": "croissant"},
        ok=True,
        payload={"status": "ambiguous", "candidates": ["Almond Croissant"]},
    )

    assert check("Shall I hold six for you?", [ambiguous]).ok is False


def test_offering_to_hold_one_is_still_blocked_under_stale_data() -> None:
    """The single-unit exemption must not open a hole in the staleness guarantee.

    Rule 3 bans every specific quantity when availability is unknown, proposals
    included, which is where the real risk of offering to hold something lives.
    """
    result = check("Shall I hold one for you?", [stale_availability()])

    assert result.ok is False
    assert "stale_override" in rules_of(result)


def test_asserting_one_exists_is_not_covered_by_the_proposal_exemption() -> None:
    """The exemption is scoped to offers. A claim about stock is still a claim."""
    assert check("We have one left.", [availability(11)]).ok is False


# ---------------------------------------------------------------------------
# False positives the eval suite found
# ---------------------------------------------------------------------------


def test_offering_to_set_something_aside_is_not_confirming_it(seeded_db: Path) -> None:
    """An offer containing confirmation words is still an offer.

    A live model answered "are you sure?" with "if you'd like, I can put one under your
    name so it's set aside" and rule 2 read "it's set aside" as a false promise. Blocking
    that stops the assistant proposing the very thing it exists to do. Rule 2 now draws
    the same assertion-versus-proposal distinction rule 1 does. Found by the eval suite.
    """
    offers = [
        "If you'd like, I can put one under your name so it's set aside.",
        "Want me to put one aside so it's reserved for you?",
        "I can hold one under your name if that helps.",
        "Shall I put one by so it's waiting when you arrive?",
    ]
    for text in offers:
        assert check(text, [availability(11)]).ok is True, f"blocked an offer: {text!r}"


def test_a_completed_claim_in_the_same_reply_is_still_caught(seeded_db: Path) -> None:
    """The narrowness of that fix, asserted from the other side.

    A reply that offers in one sentence and asserts in another must still be blocked on
    the assertion, or the fix would have opened a hole by letting any offer launder a
    claim beside it.
    """
    result = check(
        "Want me to hold one for you? You're all set, it's reserved under your name.",
        [availability(11)],
    )

    assert result.ok is False
    assert "hold_confirmation" in rules_of(result)


def test_one_for_a_person_is_an_idiom_not_a_quantity(seeded_db: Path) -> None:
    """An idiom that counts nothing: "one for a person to confirm".

    The assistant says this whenever it hands an out-of-policy request to staff, so
    reading it as a stock claim punished the correct behaviour. Found by the eval suite
    on an outside-policy scenario.
    """
    for text in [
        "That's one for the manager, let me get them to call you.",
        "It's one for our kitchen to decide on.",
    ]:
        assert check(text, [availability(11)]).ok is True, f"blocked an idiom: {text!r}"

    # The sentence as the live model actually produced it. "Fifty" is grounded because
    # the caller said it -- echoing a caller's own number back is not inventing one.
    conversation = state()
    conversation.note_customer_turn("I need fifty chocolate cakes for an event.")
    real = validate_response(
        "Fifty would need baking to order, so that's definitely one for a person to confirm.",
        [availability(11)],
        conversation,
    )
    assert real.ok is True


def test_holding_one_for_you_is_still_a_real_quantity(seeded_db: Path) -> None:
    """The idiom mask must not swallow the literal use, which differs by one word."""
    assert check("I can hold one for you.", [availability(11)]).ok is True
    assert check("We have one for you right now.", [availability(11)]).ok is False
