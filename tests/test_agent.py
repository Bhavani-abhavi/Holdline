"""The loop end to end with a scripted model: grounding, retry, tier-2 block, allergy flow."""

from __future__ import annotations

from holdline import config, inventory
from holdline.agent import Agent
from holdline.llm import FakeLLM, text_response, tool_response
from holdline.notifications import open_alerts


def make_agent(db, location_id, *responses, guardrail=True):
    llm = FakeLLM(list(responses))
    return Agent(db, llm, location_id=location_id, guardrail_enabled=guardrail), llm


def test_grounded_turn_passes_through(db, location_id):
    agent, llm = make_agent(
        db,
        location_id,
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response(
            "Marchetti's, automated assistant here. We've got 10 chocolate cakes right now. Want me to set one aside?"
        ),
    )
    result = agent.handle_message("s1", "Hi, do you have chocolate cake?")
    assert result.violations == [] and result.blocked_tier == 0
    assert result.tool_calls[0]["name"] == "check_availability"
    assert "10" in result.reply
    # The tool result reached the model as a serialised user message.
    assert llm.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_tier_1_retries_once_then_accepts_corrected_reply(db, location_id):
    agent, llm = make_agent(
        db,
        location_id,
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response("We have about a dozen."),  # ungrounded: tool said 10
        text_response("We've got 10 right now."),  # corrected
    )
    result = agent.handle_message("s1", "chocolate cake?")
    assert result.retried is True
    assert result.reply == "We've got 10 right now."
    assert result.blocked_tier == 0
    # The correction note went to the model as an operator message.
    assert "[Operator note" in llm.calls[2]["messages"][-1]["content"]
    with db.connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'guardrail_violation'"
        ).fetchone()[0]
    assert n == 1


def test_tier_1_twice_falls_back_to_safe_script(db, location_id):
    agent, _ = make_agent(
        db,
        location_id,
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response("We have a dozen."),
        text_response("Definitely twelve."),
    )
    result = agent.handle_message("s1", "chocolate cake?")
    assert result.reply == config.SAFE_FALLBACK_TEXT
    assert result.blocked_tier == 1


def test_tier_2_never_retries_and_escalates(db, location_id):
    agent, llm = make_agent(
        db,
        location_id,
        tool_response(("check_dietary_info", {"item_id": "brownie_flourless"})),
        text_response("The brownie is gluten-free, so that's safe for your son."),
        text_response("SHOULD NEVER BE REQUESTED"),
    )
    result = agent.handle_message("s1", "my son has celiac, is the brownie ok?")
    assert result.blocked_tier == 2
    assert result.reply == config.ALLERGY_ESCALATION_TEXT
    assert result.escalated is True
    assert len(llm.responses) == 1, "tier 2 must not consume a retry"
    with db.connection() as conn:
        alerts = open_alerts(conn, location_id)
    assert any(a["kind"] == "guardrail_tier2" and a["urgency"] == "urgent" for a in alerts)
    # History holds what the caller heard, not the blocked text.
    conv = agent.conversations["s1"]
    assert conv.messages[-1]["content"][-1]["text"] == config.ALLERGY_ESCALATION_TEXT


def test_allergy_declared_hold_is_pending_and_reply_hands_off(db, location_id):
    agent, _ = make_agent(
        db,
        location_id,
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response(
            "Automated assistant here. We've got 10. Any allergies I should flag before I set one aside?"
        ),
        tool_response(
            (
                "create_hold",
                {
                    "item_id": "cake_choc_9",
                    "quantity": 1,
                    "pickup_time": "5pm",
                    "dietary_note": "my daughter's allergic to peanuts",
                },
            )
        ),
        text_response(
            "I've put a soft hold on one and someone from the bakery will call you back to confirm. Best number?"
        ),
    )
    agent.handle_message("s1", "do you have chocolate cake?")
    result = agent.handle_message(
        "s1", "yes please, around five. My daughter's allergic to peanuts."
    )
    assert result.blocked_tier == 0
    assert result.tool_calls[0]["result"]["status"] == "pending_approval"
    assert agent.conversations["s1"].state.dietary_severity == "allergy"


def test_stale_data_blocks_numbers(db, location_id):
    inventory.force_stale(db, location_id)
    agent, _ = make_agent(
        db,
        location_id,
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response("We usually have around 8, want me to hold one?"),
        text_response("I can't see a reliable count right now. Can someone call you back?"),
    )
    result = agent.handle_message("s1", "chocolate cake?")
    assert result.retried and result.blocked_tier == 0
    assert "8" not in result.reply


def test_guardrail_disabled_observes_but_delivers(db, location_id):
    agent, _ = make_agent(
        db,
        location_id,
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response("We have a dozen."),
        guardrail=False,
    )
    result = agent.handle_message("s1", "chocolate cake?")
    assert result.reply == "We have a dozen."
    assert result.violations and result.blocked_tier == 0


def test_llm_failure_gives_safe_fallback(db, location_id):
    agent, _ = make_agent(db, location_id)  # nothing queued -> LLMUnavailableError
    result = agent.handle_message("s1", "hello")
    assert result.reply == config.SAFE_FALLBACK_TEXT


def test_tool_loop_cap(db, location_id):
    responses = [
        tool_response(("get_store_info", {})) for _ in range(config.AGENT_MAX_TOOL_LOOPS + 2)
    ]
    agent, llm = make_agent(db, location_id, *responses)
    result = agent.handle_message("s1", "hours?")
    assert result.reply == config.SAFE_FALLBACK_TEXT
    assert len(llm.calls) == config.AGENT_MAX_TOOL_LOOPS
