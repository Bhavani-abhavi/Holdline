"""The tool layer: one dispatch, schemas from one source, and nothing that raises."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from holdline import config, db, inventory, tools

from helpers import CAKE, LOCATION_ID, alerts_of_kind, count_live_holds, events_of_kind, set_stock

# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------


def test_availability_resolves_a_spoken_name_and_reports_a_number(seeded_db: Path) -> None:
    """The tool a caller's first question always reaches."""
    result = tools.dispatch("check_availability", {"item_query": "chocolate cake"})

    assert result.ok is True
    assert result.payload["status"] == "known"
    assert result.payload["quantity"] == 11
    assert result.payload["item_id"] == CAKE


def test_availability_asks_which_one_rather_than_guessing(seeded_db: Path) -> None:
    """An ambiguous request comes back as candidates, so the assistant can ask."""
    result = tools.dispatch("check_availability", {"item_query": "croissant"})

    assert result.payload["status"] == "ambiguous"
    assert len(result.payload["candidates"]) >= 2
    assert "quantity" not in result.payload


def test_availability_says_so_when_the_bakery_does_not_carry_it(seeded_db: Path) -> None:
    """Distinct from sold out, and distinct from unknown."""
    result = tools.dispatch("check_availability", {"item_query": "something nice"})

    assert result.payload["status"] == "no_such_item"


def test_availability_returns_unknown_rather_than_a_stale_number(seeded_db: Path) -> None:
    """The staleness gate reaches the tool layer intact, with no number attached."""
    inventory.force_stale(LOCATION_ID)

    result = tools.dispatch("check_availability", {"item_query": "chocolate cake"})

    assert result.payload["status"] == "unknown"
    assert result.payload["reason"] == "stale_data"
    assert "quantity" not in result.payload


# ---------------------------------------------------------------------------
# list_menu
# ---------------------------------------------------------------------------


def test_the_menu_lists_every_item_with_a_price(seeded_db: Path) -> None:
    """Twelve items, grouped by category, each with a price a caller can hear."""
    result = tools.dispatch("list_menu", {})

    items = [item for group in result.payload["menu"].values() for item in group]
    assert len(items) == 12
    assert all(item["price"].startswith("$") for item in items)


def test_the_menu_never_carries_a_quantity(seeded_db: Path) -> None:
    """A menu has no freshness guarantee, so any number in it would be unpromisable.

    Enforced by construction rather than requested in a prompt: the payload is built
    from name, category and price, so there is no stock figure to leak.
    """
    result = tools.dispatch("list_menu", {})
    rendered = json.dumps(result.payload)

    for forbidden in ("quantity", "on_hand", "sellable", "available", "stock", "safety_buffer"):
        assert forbidden not in rendered


def test_the_menu_does_not_change_when_stock_moves(seeded_db: Path) -> None:
    """The strongest form of the previous test: selling out changes nothing here."""
    before = tools.dispatch("list_menu", {}).payload

    set_stock(CAKE, on_hand=0)
    after = tools.dispatch("list_menu", {}).payload

    assert before == after


def test_prices_are_strings_so_they_cannot_be_mistaken_for_counts(seeded_db: Path) -> None:
    """Also keeps them out of the guardrail's grounded set, where they do not belong."""
    result = tools.dispatch("list_menu", {})

    assert result.grounded_numbers() == set()


# ---------------------------------------------------------------------------
# check_dietary_info
# ---------------------------------------------------------------------------


def test_dietary_info_returns_the_declaration_and_the_disclaimer(seeded_db: Path) -> None:
    """What the kitchen declared, plus the text saying it is not a safety verdict."""
    result = tools.dispatch("check_dietary_info", {"item_id": "chocolate cake"})

    assert "wheat" in result.payload["contains"]
    assert "tree nuts" in result.payload["may_contain"]
    assert "not a safety assessment" in result.payload["disclaimer"].lower()


def test_dietary_info_refuses_to_guess_which_item_was_meant(seeded_db: Path) -> None:
    """The one tool where a wrong guess could kill somebody.

    An almond croissant and a butter croissant are different answers to a caller with
    a nut allergy, so a near-tie returns candidates instead of a best guess.
    """
    result = tools.dispatch("check_dietary_info", {"item_id": "croissant"})

    assert result.payload["status"] == "ambiguous"
    assert "contains" not in result.payload


def test_dietary_info_never_reports_an_empty_ingredient_list_for_an_unknown_item(
    seeded_db: Path,
) -> None:
    """An empty `contains` reads as "contains nothing", the worst possible answer."""
    result = tools.dispatch("check_dietary_info", {"item_id": "flying saucer"})

    assert result.payload["status"] == "no_such_item"
    assert "contains" not in result.payload


# ---------------------------------------------------------------------------
# create_hold
# ---------------------------------------------------------------------------


def test_a_hold_is_created_from_a_spoken_item_and_a_spoken_time(seeded_db: Path) -> None:
    """Both halves of the spoken layer, exercised through the tool a caller triggers."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    result = tools.dispatch(
        "create_hold",
        {
            "item_id": "chocolate cake",
            "quantity": 1,
            "pickup_time": "in twenty minutes",
            "customer_name": "Dana",
        },
    )

    assert result.payload["ok"] is True
    assert result.payload["status"] == "active"
    assert result.payload["pickup_interpretation"] == "in 20 minutes"
    assert count_live_holds(CAKE) == 1


def test_an_unreadable_pickup_time_is_a_question_not_a_hold(seeded_db: Path) -> None:
    """A hold placed against a misread time expires before the caller arrives."""
    result = tools.dispatch(
        "create_hold",
        {"item_id": "chocolate cake", "quantity": 1, "pickup_time": "whenever really"},
    )

    assert result.payload["ok"] is False
    assert result.payload["reason"] == "pickup_time_unclear"
    assert count_live_holds(CAKE) == 0


def test_an_ambiguous_item_does_not_get_a_hold(seeded_db: Path) -> None:
    """Reserving the wrong croissant is worse than asking which one."""
    result = tools.dispatch("create_hold", {"item_id": "croissant", "quantity": 1})

    assert result.payload["ok"] is False
    assert result.payload["reason"] == "ambiguous_item"
    assert result.payload["candidates"]


def test_a_dietary_note_routes_the_hold_to_a_human(seeded_db: Path) -> None:
    """Policy is in the handler, not the prompt: the tool itself decides this."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    result = tools.dispatch(
        "create_hold",
        {
            "item_id": "chocolate cake",
            "quantity": 1,
            "pickup_time": "in an hour",
            "dietary_note": "my son is allergic to peanuts",
        },
    )

    assert result.payload["status"] == "pending_approval"
    assert result.payload["dietary_severity"] == "allergy"
    assert len(alerts_of_kind("allergy_callback")) == 1


def test_a_caller_saying_they_have_no_allergies_is_not_escalated(seeded_db: Path) -> None:
    """The commonest answer to the assistant's own question must not raise an alarm.

    "No allergies" contains the substring "allerg", so a naive reading escalates every
    caller who was asked and said no -- which would make the whole feature useless.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    result = tools.dispatch(
        "create_hold",
        {
            "item_id": "chocolate cake",
            "quantity": 1,
            "pickup_time": "in an hour",
            "dietary_note": "no allergies",
        },
    )

    assert result.payload["dietary_severity"] == "none"
    assert alerts_of_kind("allergy_callback") == []
    # And the hold activates rather than queueing. Routing an explicit "no allergies"
    # to a manager would send the commonest order in the business to the approval
    # queue, which is not caution -- it is a queue nobody reads.
    assert result.payload["status"] == "active"
    assert result.payload["approval_reasons"] == []


def test_an_as_soon_as_possible_pickup_is_not_refused_as_being_in_the_past(
    seeded_db: Path,
) -> None:
    """The commonest pickup case in the business, and it was broken.

    The tool resolves "on my way" to the instant it ran; the ledger then reads the
    clock again a moment later. Without a tolerance the check fired on its own latency
    and every such hold came back `pickup_in_past`. See DEFECTS.md entry 2.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    for said in ("", "now", "on my way", "right away", "as soon as possible"):
        result = tools.dispatch(
            "create_hold",
            {"item_id": "chocolate cake", "quantity": 1, "pickup_time": said},
        )
        assert result.payload["ok"] is True, f"{said!r} was refused: {result.payload}"
        assert result.payload["reason"] is None


def test_a_hold_beyond_what_is_sellable_is_refused_with_a_reason(seeded_db: Path) -> None:
    """The refusal carries something the assistant can say out loud."""
    set_stock(CAKE, on_hand=3, safety_buffer=1)

    result = tools.dispatch(
        "create_hold",
        {"item_id": "chocolate cake", "quantity": 10, "pickup_time": "in an hour"},
    )

    assert result.payload["ok"] is False
    assert result.payload["reason"] == "insufficient"


# ---------------------------------------------------------------------------
# The remaining tools
# ---------------------------------------------------------------------------


def test_a_hold_can_be_cancelled_through_the_tool(seeded_db: Path) -> None:
    """A caller changing their mind is a normal thing to do on a phone."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    created = tools.dispatch(
        "create_hold",
        {"item_id": "chocolate cake", "quantity": 1, "pickup_time": "in an hour"},
    )

    result = tools.dispatch("cancel_hold", {"hold_id": created.payload["hold_id"]})

    assert result.payload["ok"] is True
    assert count_live_holds(CAKE) == 0


def test_store_info_answers_hours_address_and_phone(seeded_db: Path) -> None:
    """So the assistant reads them rather than inventing them."""
    result = tools.dispatch("get_store_info", {})

    assert result.payload["address"]
    assert result.payload["phone"]
    assert "monday" in result.payload["hours"]


def test_escalating_reaches_staff_and_keeps_the_callback_number(seeded_db: Path) -> None:
    """`staff_alerts` is an operational queue, so the real number belongs in it."""
    result = tools.dispatch(
        "escalate_to_staff",
        {
            "reason": "caller asked whether the brownie is safe for coeliac disease",
            "callback_number": "+13125550188",
            "urgency": "urgent",
        },
    )

    assert result.payload["ok"] is True
    alerts = alerts_of_kind("escalation")
    assert len(alerts) == 1
    assert alerts[0]["urgency"] == "urgent"


def test_an_unknown_urgency_is_clamped_rather_than_stored(seeded_db: Path) -> None:
    """An urgency the staff page cannot colour is an alert nobody reads correctly."""
    tools.dispatch("escalate_to_staff", {"reason": "something odd", "urgency": "catastrophic"})

    assert alerts_of_kind("escalation")[0]["urgency"] == "action"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dispatch_never_raises_for_an_unknown_tool(seeded_db: Path) -> None:
    """A model calling a tool that does not exist must not end the call."""
    result = tools.dispatch("order_a_pony", {"colour": "white"})

    assert result.ok is False
    assert "unknown tool" in (result.error or "")


def test_dispatch_never_raises_for_bad_arguments(seeded_db: Path) -> None:
    """Strict schemas should prevent this; if it happens the turn still degrades."""
    result = tools.dispatch("check_availability", {"wrong_argument": "chocolate cake"})

    assert result.ok is False
    assert "invalid arguments" in (result.error or "")


def test_dispatch_turns_a_domain_refusal_into_a_result(seeded_db: Path) -> None:
    """A business rule saying no is expected, and the assistant can explain it."""
    result = tools.dispatch("check_dietary_info", {"item_id": "flying saucer"})

    assert result.ok is True  # the tool answered; the answer is "we don't have that"
    assert result.payload["status"] == "no_such_item"


def test_a_handler_that_raises_becomes_a_result_rather_than_ending_the_call(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last line of defence. A live phone call must not die inside a tool.

    Domain errors are caught by name; this covers everything else, which is the case
    nobody can enumerate in advance.
    """

    def exploding(**_kwargs: object) -> dict[str, object]:
        raise ZeroDivisionError("something nobody predicted")

    # ToolSpec is frozen, so swap the whole entry rather than mutating the handler.
    monkeypatch.setitem(
        tools.SPECS_BY_NAME,
        "get_store_info",
        dataclasses.replace(tools.SPECS_BY_NAME["get_store_info"], handler=exploding),
    )

    result = tools.dispatch("get_store_info", {})

    assert result.ok is False
    assert "internal error" in (result.error or "")


def test_every_tool_call_is_written_to_the_event_log(seeded_db: Path) -> None:
    """What the glass box renders and what Phase 8 joins on."""
    tools.dispatch("check_availability", {"item_query": "chocolate cake"}, call_id="call_1")

    recorded = events_of_kind("tool_call")
    assert len(recorded) == 1
    detail = json.loads(str(recorded[0]["detail"]))
    assert detail["tool"] == "check_availability"
    assert detail["ok"] is True


def test_a_phone_number_in_the_arguments_is_redacted_in_the_event_log(
    seeded_db: Path,
) -> None:
    """The event log is exported to Parquet in Phase 8; the ledger is not."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    tools.dispatch(
        "create_hold",
        {
            "item_id": "chocolate cake",
            "quantity": 1,
            "pickup_time": "in an hour",
            "customer_phone": "+13125550188",
        },
    )

    rendered = str(events_of_kind("tool_call"))
    assert "3125550188" not in rendered
    assert "0188" in rendered  # last four kept for recognisability


def test_the_call_id_and_channel_reach_the_ledger(seeded_db: Path) -> None:
    """Tracing one conversation end to end depends on this being threaded through."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    tools.dispatch(
        "create_hold",
        {"item_id": "chocolate cake", "quantity": 1, "pickup_time": "in an hour"},
        call_id="call_xyz",
        channel="phone",
    )

    row = db.query_one("SELECT call_id FROM holds ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert row["call_id"] == "call_xyz"


# ---------------------------------------------------------------------------
# Schemas: one source, two renderings
# ---------------------------------------------------------------------------


def test_all_seven_tools_are_defined(seeded_db: Path) -> None:
    """The set the specification asks for, no more and no fewer."""
    assert {spec.name for spec in tools.TOOL_SPECS} == {
        "check_availability",
        "list_menu",
        "check_dietary_info",
        "create_hold",
        "cancel_hold",
        "get_store_info",
        "escalate_to_staff",
    }


def test_both_renderings_come_from_the_same_source(seeded_db: Path) -> None:
    """Phase 6's phone path must not be able to drift from the browser's.

    A hand-maintained second copy is a copy that drifts, and a drifted phone schema is
    a phone path that silently behaves differently.
    """
    anthropic_names = [tool["name"] for tool in tools.anthropic_tool_definitions()]
    openai_names = [tool["function"]["name"] for tool in tools.openai_tool_definitions()]

    assert anthropic_names == openai_names == [spec.name for spec in tools.TOOL_SPECS]


def test_both_renderings_agree_on_required_arguments(seeded_db: Path) -> None:
    """A required field on one channel and optional on the other is a silent divergence."""
    for anthropic_tool, openai_tool in zip(
        tools.anthropic_tool_definitions(), tools.openai_tool_definitions(), strict=True
    ):
        assert (
            anthropic_tool["input_schema"]["required"]
            == openai_tool["function"]["parameters"]["required"]
        )


def test_every_tool_is_strict_and_closed(seeded_db: Path) -> None:
    """Strict mode guarantees the arguments validate, which the handlers rely on.

    In a system whose central claim is groundedness, a malformed quantity argument is
    not something to be tolerant about.
    """
    for tool in tools.anthropic_tool_definitions():
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False


def test_every_tool_has_a_description_the_model_can_act_on(seeded_db: Path) -> None:
    """A tool nobody can tell when to call is a tool that gets called wrongly."""
    for spec in tools.TOOL_SPECS:
        assert len(spec.description) > 40


def test_the_dietary_tool_warns_against_its_own_misuse(seeded_db: Path) -> None:
    """The boundary is stated where the model reads it, not only in the system prompt."""
    spec = tools.SPECS_BY_NAME["check_dietary_info"]

    assert "not a safety assessment" in spec.description.lower()
    assert "never" in spec.description.lower()


def test_injected_arguments_are_not_offered_to_the_model(seeded_db: Path) -> None:
    """`call_id` and `channel` come from the runtime, not from the conversation.

    Letting the model supply them would let a caller's words influence which call a
    hold is attributed to.
    """
    for spec in tools.TOOL_SPECS:
        for injected in spec.injected:
            assert injected not in spec.properties


def test_the_default_location_is_the_only_one_tools_touch(seeded_db: Path) -> None:
    """The application serves one location; the schema carries many. Tools serve one."""
    result = tools.dispatch("get_store_info", {})

    assert result.payload["name"]
    assert config.DEFAULT_LOCATION_ID == LOCATION_ID


#: JSON-Schema keywords that `strict: true` rejects outright. A single one of these
#: anywhere in the tool list makes the whole request fail with a 400, so every turn of
#: every conversation breaks at once -- and nothing offline notices, because the schemas
#: are only validated by the API. This list was written after a live call failed on
#: `"minimum": 1`. See DEFECTS.md entry 7.
STRICT_MODE_REJECTS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "uniqueItems",
    "default",
)


def test_no_tool_schema_uses_a_keyword_strict_mode_rejects(seeded_db: Path) -> None:
    """Constraints belong in the handler, not in a schema the API will not accept.

    This is the only class of bug in the project that offline tests cannot see, because
    the schema is validated by the API rather than by us. It is also the most total: one
    bad keyword 400s the entire request, so every conversation fails identically and the
    symptom looks like a provider outage rather than a schema mistake.
    """

    def walk(node: object, path: str) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in STRICT_MODE_REJECTS:
                    found.append(f"{path}.{key}")
                found.extend(walk(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found.extend(walk(value, f"{path}[{index}]"))
        return found

    offenders = walk(tools.anthropic_tool_definitions(), "tools")

    assert not offenders, f"strict mode will reject: {offenders}"


def test_a_quantity_floor_is_enforced_by_the_handler_not_the_schema(seeded_db: Path) -> None:
    """The constraint still exists; it just lives where it can be expressed properly.

    A schema can say "at least 1". It cannot say "at least 1 and no more than is
    sellable right now", which is the rule that actually matters -- so the floor belongs
    next to the ceiling, in the ledger.
    """
    for bad in (0, -3):
        result = tools.dispatch(
            "create_hold",
            {"item_id": "chocolate cake", "quantity": bad, "pickup_time": "in an hour"},
        )
        assert result.payload["ok"] is False
        assert result.payload["reason"] == "invalid_quantity"
