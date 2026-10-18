"""Both tiers, every false-positive case, assertions versus proposals."""

from __future__ import annotations

import pytest

from holdline.guardrails import customer_numbers, extract_quantities, validate_response
from holdline.state import ConversationState
from holdline.tools import ToolResult


def _state(**kw) -> ConversationState:
    base = ConversationState(call_id="c", channel="browser", location_id="loc")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def avail(qty: int | None, item_id="cake_choc_9", name='Chocolate Cake 9"') -> ToolResult:
    if qty is None:
        return ToolResult(
            "check_availability",
            {"item_query": "x"},
            {
                "status": "unknown",
                "item_id": item_id,
                "item_name": name,
                "reason": "stale_data",
                "age_seconds": 3600,
            },
        )
    return ToolResult(
        "check_availability",
        {"item_query": "x"},
        {
            "status": "known",
            "item_id": item_id,
            "item_name": name,
            "quantity": qty,
            "as_of": "2026-10-18T15:00:00+00:00",
        },
    )


def hold(ok=True, status="active", qty=1) -> ToolResult:
    out = (
        {
            "ok": ok,
            "hold_id": 7,
            "status": status,
            "expires_at": "x",
            "requires_approval": status != "active",
            "approval_reasons": [],
        }
        if ok
        else {"ok": False, "reason": "insufficient", "message": "Only 0 sellable right now."}
    )
    return ToolResult(
        "create_hold",
        {"item_id": "cake_choc_9", "quantity": qty, "pickup_time": "5pm"},
        out,
        ok=True,
    )


def dietary(contains, may=()) -> ToolResult:
    return ToolResult(
        "check_dietary_info",
        {"item_id": "x"},
        {
            "item_id": "x",
            "item_name": "x",
            "contains": list(contains),
            "may_contain": list(may),
            "dietary_flags": [],
            "policy": "...",
        },
    )


# --- Tier 1: quantity grounding ----------------------------------------------------------


def test_grounded_numeral_passes():
    assert validate_response("We've got 11 chocolate cakes right now.", [avail(11)], _state()).ok


def test_grounded_number_word_passes():
    assert validate_response(
        "We do, eleven nine-inch chocolate cakes right now.", [avail(11)], _state()
    ).ok


def test_ungrounded_numeral_is_tier_1():
    result = validate_response("We have 12 chocolate cakes.", [avail(11)], _state())
    assert [v.rule for v in result.violations] == ["ungrounded_quantity"]
    assert result.tier == 1


def test_ungrounded_number_word_is_tier_1():
    result = validate_response("We have a dozen left.", [avail(11)], _state())
    assert result.tier == 1 and result.violations[0].rule == "ungrounded_quantity"


def test_no_tool_result_at_all_and_a_number_is_ungrounded():
    assert validate_response("Sure, we have five.", [], _state()).tier == 1


def test_customer_stated_number_can_be_echoed():
    st = _state(customer_numbers={2})
    assert validate_response("Two chocolate cakes, got it. Let me check.", [], st).ok


def test_customer_numbers_extraction():
    assert customer_numbers("I'd like two cakes, maybe three") == {2, 3}
    assert customer_numbers("can I get a dozen") == {12}


# --- Proposals -----------------------------------------------------------------------------


def test_proposal_within_availability_is_allowed():
    text = "We do, eleven right now. Want me to set one aside?"
    assert validate_response(text, [avail(11)], _state()).ok


def test_proposal_above_availability_is_violation():
    text = "We have 2 left. Shall I hold five for you?"
    result = validate_response(text, [avail(2)], _state())
    assert [v.rule for v in result.violations] == ["proposal_exceeds_availability"]


def test_proposal_with_no_known_availability_is_violation():
    result = validate_response("Want me to set two aside?", [], _state())
    assert result.violations[0].rule == "proposal_exceeds_availability"


# --- Stale override -------------------------------------------------------------------------


def test_stale_forbids_any_number_even_as_offer():
    text = "I'm not certain right now, but we usually have about 10. Want me to hold one?"
    result = validate_response(text, [avail(None)], _state())
    rules = {v.rule for v in result.violations}
    assert rules == {"stale_quantity"}


def test_stale_with_no_number_is_fine():
    text = "I can't see a reliable count right now. Can I have someone call you back?"
    assert validate_response(text, [avail(None)], _state(availability_known=False)).ok


# --- False positives ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We're open until 7pm, so 5:45pm works great.",
        "That one's $24, or twenty-four dollars.",
        "I have your number as 312-555-0142.",
        "It's October 18th, so the 20th would be Tuesday.",
        "Your hold number is 12 and the reference is #45.",
        "We're at 2140 N Halsted Street.",
        "One moment while I check.",
        "It'll be ready in 45 minutes, about ten minutes from now actually.",
        'The nine-inch is the only size we do, a 9" round.',
        "Is it the chocolate one or the lemon one you're after?",
        "I'll have someone call you in a couple of minutes.",
        "See you at five!",
        "We close at 7 tonight.",
    ],
)
def test_false_positive_free(text):
    result = validate_response(text, [avail(11)], _state())
    assert result.ok, result.violations


def test_extract_quantities_classifies_proposal():
    mentions = extract_quantities("We have 11. Want me to set one aside?")
    assert [(m.value, m.is_proposal) for m in mentions] == [(11, False), (1, True)]


# --- Hold confirmation ----------------------------------------------------------------------


def test_confirmation_without_hold_is_violation():
    result = validate_response("You're all set, I've set one aside for you.", [avail(11)], _state())
    assert "unconfirmed_hold" in {v.rule for v in result.violations}


def test_confirmation_after_rejected_hold_is_violation():
    result = validate_response("You're all set!", [avail(0), hold(ok=False)], _state())
    assert "unconfirmed_hold" in {v.rule for v in result.violations}


def test_confirmation_with_successful_hold_passes():
    text = "You're all set, one chocolate cake under your name for five."
    assert validate_response(text, [avail(11), hold()], _state()).ok


def test_pending_hold_presented_as_guaranteed_is_violation():
    text = "You're all set, that's confirmed."
    result = validate_response(
        text, [avail(11), hold(status="pending_approval", qty=3)], _state(customer_numbers={3})
    )
    assert "pending_presented_as_confirmed" in {v.rule for v in result.violations}


def test_pending_hold_described_honestly_passes():
    text = "I've put that request in for three and a person will confirm with you shortly."
    assert validate_response(text, [avail(11), hold(status="pending_approval", qty=3)], _state()).ok


# --- Tier 2 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Good news, that one's peanut-free.",
        "It's safe for her.",
        "That should be fine for a nut allergy.",
        "It doesn't contain any nuts.",
        "There are no peanuts in the brownie.",
        "The brownie is gluten-free, so you're good.",
        "Nothing in it you'd need to worry about.",
        "It's vegan.",
        "The sourdough is made without eggs.",
    ],
)
def test_absence_claims_are_tier_2_even_with_dietary_lookup(text):
    results = [dietary(["eggs", "dairy"], may=["wheat"])]
    result = validate_response(text, results, _state())
    assert result.tier == 2, result.violations
    assert any(v.rule == "allergen_absence_claim" for v in result.violations)


@pytest.mark.parametrize(
    "text",
    [
        "I can't say it's nut-free over the phone, but I can tell you what's in it.",
        "I'm not able to promise it's safe for her, so I'll have someone call you.",
        "I can't confirm whether it's gluten-free; the kitchen will check.",
        "It contains eggs and dairy, and may contain wheat from shared equipment.",
    ],
)
def test_disclaimers_and_presence_statements_pass_tier_2(text):
    results = [dietary(["eggs", "dairy"], may=["wheat"])]
    result = validate_response(text, results, _state())
    assert result.tier < 2, result.violations


def test_presence_claim_without_lookup_is_tier_1_not_2():
    result = validate_response("It contains walnuts.", [], _state())
    assert result.tier == 1 and result.violations[0].rule == "ungrounded_presence"


def test_presence_claim_matching_lookup_passes():
    assert validate_response(
        "It contains walnuts and dairy.", [dietary(["tree nuts", "dairy"])], _state()
    ).ok


def test_allergy_declared_and_active_hold_is_tier_2():
    st = _state(dietary_severity="allergy", declared_dietary="peanut allergy")
    result = validate_response(
        "I've set one aside and someone will call you back.", [avail(11), hold(status="active")], st
    )
    assert any(v.rule == "allergy_hold_activated" and v.tier == 2 for v in result.violations)


def test_allergy_declared_confirmation_without_handoff_is_tier_2():
    st = _state(dietary_severity="allergy", declared_dietary="peanut allergy")
    result = validate_response(
        "You're all set, see you at five.", [avail(11), hold(status="pending_approval")], st
    )
    assert any(v.rule == "autonomous_allergy_commitment" for v in result.violations)


def test_allergy_declared_with_handoff_language_passes():
    st = _state(dietary_severity="allergy", declared_dietary="peanut allergy")
    text = "I've put a soft hold on one and someone from the bakery will call you back to confirm."
    assert validate_response(text, [avail(11), hold(status="pending_approval")], st).ok
