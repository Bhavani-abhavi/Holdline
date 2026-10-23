"""Cases ported from the earlier Antigravity build's defect log and tests.

Each test names the defect it guards against (legacy/antigravity/DEFECTS.md).
The phrasings are the ones a live caller used, not the ones the spec used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from holdline import clock
from holdline.dietary import assess_request
from holdline.guardrails import validate_response
from holdline.holds import create_hold, needs_approval
from holdline.state import ConversationState
from holdline.tools import TOOL_SCHEMAS, ToolResult, parse_pickup
from tests.test_guardrails import avail, dietary, hold

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)  # 10:00 Chicago


def _state(**kw) -> ConversationState:
    st = ConversationState(call_id="c", channel="browser", location_id="loc")
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# --- Defects 3 and 8: denials, clause by clause ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "no allergies",
        "No allergies.",
        "none",
        "nope, all good",
        "Yes, around five. No allergies.",
        "Yes please, hold one for me. I'm Dana, I'll be there in about twenty minutes, and no allergies.",
        "no dietary restrictions",
        "none that I know of",
    ],
)
def test_a_denial_is_not_an_allergy(text):
    assert assess_request(text, direct_answer=True).severity == "none"


def test_a_denial_in_one_clause_does_not_cancel_a_declaration_in_another():
    assert assess_request("no nut allergies but she can't have dairy").severity == "allergy"


def test_an_ordinary_question_is_not_a_dietary_preference():
    assert assess_request("Do you have chocolate cake?", direct_answer=False).severity == "none"
    assert assess_request("That's fine, five o'clock works", direct_answer=False).severity == "none"


def test_an_unparseable_direct_answer_still_reaches_the_kitchen():
    assert (
        assess_request("she likes it extra chocolatey", direct_answer=True).severity == "preference"
    )


# --- Defect 9: allergen names inside ordinary words ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I'll be there in twenty minutes",
        "give me a minute",
        "we're just round the corner",
        "in about ten minutes",
        "I can't do five, make it four",
    ],
)
def test_ordinary_turns_do_not_declare_an_allergy(text):
    assert assess_request(text, direct_answer=False).severity == "none"


# --- Defect 6: "no allergies" must not send the order to a manager ----------------------------


def test_a_caller_saying_they_have_no_allergies_is_not_escalated(db, location_id):
    with db.connection() as conn:
        reasons = needs_approval(
            conn,
            location_id,
            "cake_choc_9",
            1,
            clock.now() + timedelta(hours=1),
            "and no allergies",
            clock.now(),
        )
    assert reasons == []
    result = create_hold(
        db,
        location_id=location_id,
        item_id="cake_choc_9",
        quantity=1,
        pickup_at=clock.now() + timedelta(hours=1),
        call_id="c",
        dietary_note="no allergies",
    )
    assert result.status == "active"


# --- Defect 2: "I'm on my way" -----------------------------------------------------------------


@pytest.mark.parametrize("said", ["now", "right away", "on my way", "I'm on my way", "asap"])
def test_coming_now_is_a_pickup_time(said):
    assert parse_pickup(said, NOW, TZ) == NOW


def test_coming_now_is_not_refused_as_in_the_past(db, location_id):
    result = create_hold(
        db,
        location_id=location_id,
        item_id="cake_choc_9",
        quantity=1,
        pickup_at=clock.now() - timedelta(seconds=5),
        call_id="c",
    )
    assert result.ok, result.reason


def test_no_time_at_all_means_shortly():
    assert parse_pickup("", NOW, TZ) == NOW + timedelta(minutes=15)


@pytest.mark.parametrize(
    "said,minutes",
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
        ("in about an hour", 60),
    ],
)
def test_relative_times(said, minutes):
    assert parse_pickup(said, NOW, TZ) == NOW + timedelta(minutes=minutes)


@pytest.mark.parametrize(
    "said,hour,minute",
    [
        ("5:30pm", 17, 30),
        ("5:30 pm", 17, 30),
        ("17:30", 17, 30),
        ("half past four", 16, 30),
        ("quarter past six", 18, 15),
        ("quarter to six", 17, 45),
        ("noon", 12, 0),
        ("2pm", 14, 0),
        ("five", 17, 0),
        ("around five", 17, 0),
        ("eight", 8, 0),
        ("5ish", 17, 0),
    ],
)
def test_clock_times_land_where_a_human_would_read_them(said, hour, minute):
    local = parse_pickup(said, NOW, TZ).astimezone(TZ)
    assert (local.hour, local.minute) == (hour, minute)


@pytest.mark.parametrize(
    "said,hour",
    [
        ("tomorrow at two", 14),
        ("tomorrow at five", 17),
        ("tomorrow at eight", 8),
        ("tomorrow at 2pm", 14),
    ],
)
def test_tomorrow_uses_the_shops_opening_envelope(said, hour):
    local = parse_pickup(said, NOW, TZ).astimezone(TZ)
    assert local.hour == hour and local.date() == (NOW.astimezone(TZ) + timedelta(days=1)).date()


# --- Defect 10: the guardrail must not punish care ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We've got eleven right now, though it can shift if someone buys one at the counter.",
        "That's eleven, but somebody could take one before you arrive.",
        "Eleven at the moment. People do grab one on the way home.",
    ],
)
def test_a_third_party_buying_one_is_a_hedge_not_a_stock_claim(text):
    assert validate_response(text, [avail(11)], _state()).ok


def test_the_shop_claiming_to_have_one_is_still_checked():
    assert validate_response("We have one left.", [avail(11)], _state()).ok is False
    assert validate_response("There's only one left.", [avail(11)], _state()).ok is False


def test_offering_to_hold_one_works_when_the_item_is_still_ambiguous():
    ambiguous = ToolResult(
        "check_availability",
        {"item_query": "croissant"},
        {
            "status": "ambiguous",
            "candidates": [
                {"item_id": "a", "item_name": "Almond Croissant"},
                {"item_id": "b", "item_name": "Butter Croissant"},
            ],
        },
    )
    assert validate_response(
        "We've got almond and butter. Shall I hold one for you?", [ambiguous], _state()
    ).ok


def test_offering_more_than_one_still_needs_a_ceiling():
    assert validate_response("Shall I hold two for you?", [], _state()).ok is False


def test_offering_to_hold_one_is_still_blocked_under_stale_data():
    assert validate_response("Shall I hold one for you?", [avail(None)], _state()).ok is False


def test_the_readme_transcript_passes_exactly_as_written():
    text = (
        "We do, eleven nine-inch chocolate cakes right now. They tend to go by early "
        "evening. Want me to set one aside?"
    )
    assert validate_response(text, [avail(11)], _state()).ok


# --- Reassurance is only a safety claim once a dietary need is on the table ----------------------


def test_reassurance_is_blocked_once_a_dietary_need_has_been_declared():
    st = _state(dietary_severity="allergy", declared_dietary="peanut allergy")
    result = validate_response("She'll be fine with that one.", [dietary(["wheat"])], st)
    assert result.tier == 2


def test_the_same_reassurance_in_an_ordinary_booking_is_allowed():
    assert validate_response("She'll be fine with that one.", [avail(11)], _state()).ok


def test_suitable_for_an_occasion_is_not_a_safety_claim():
    assert validate_response("That one's suitable for a birthday party.", [avail(11)], _state()).ok


def test_a_price_followed_by_a_claim_keeps_its_own_sentence():
    # Defect 1 in the earlier build: masking a price merged two sentences.
    result = validate_response("It costs $24.00. We have thirty left.", [avail(11)], _state())
    assert [v.rule for v in result.violations] == ["ungrounded_quantity"]
    assert "thirty" in result.violations[0].excerpt


def test_a_sentence_mixing_a_price_a_time_and_a_grounded_quantity_passes():
    text = "We have eleven at $42.00 each, and I can hold one until 5:45pm."
    assert validate_response(text, [avail(11), hold()], _state()).ok


# --- Defect 7: schema keywords the API rejects ---------------------------------------------------

UNSUPPORTED_UNDER_STRICT = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "pattern",
    "format",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
}


def _walk(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in UNSUPPORTED_UNDER_STRICT:
                found.add(k)
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def test_no_tool_schema_uses_a_keyword_strict_mode_rejects():
    found: set[str] = set()
    for schema in TOOL_SCHEMAS:
        _walk(schema["input_schema"], found)
    assert found == set(), f"offline tests cannot see a 400 from these: {found}"
