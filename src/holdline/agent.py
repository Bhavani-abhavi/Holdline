"""The conversation loop and persona (manual implementation).

send -> execute requested tools -> loop (max AGENT_MAX_TOOL_LOOPS) -> validate
the final text through guardrails.validate_response -> return.

# The system prompt DOCUMENTS intent. guardrails.py ENFORCES it.
# Warmth and grounding are in tension: a chattier model improvises more.
# That tension is exactly why enforcement is code and not a longer prompt.

Phase 5 ports this loop to LangGraph as agent_graph.py and keeps this file as
agent_manual.py for comparison. Nothing here is deleted then.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from holdline import clock, config, events
from holdline.db import Database
from holdline.dietary import assess_request
from holdline.exceptions import LLMUnavailableError
from holdline.guardrails import ValidationResult, customer_numbers, validate_response
from holdline.llm import LLMClient, Usage
from holdline.logging_setup import bind_call, clear_call, get_logger
from holdline.notifications import fan_out
from holdline.prompts import SYSTEM_PROMPT_STATIC
from holdline.state import ConversationState
from holdline.tools import TOOL_SCHEMAS, ToolContext, ToolResult, dispatch, result_to_content

log = get_logger(__name__)


@dataclass
class TurnResult:
    """What the interface layer needs after one caller message."""

    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    blocked_tier: int = 0
    escalated: bool = False
    retried: bool = False
    usage: Usage = field(default_factory=Usage)
    raw_text: str | None = None  # what the model said before any substitution
    # Violations found in the reply the caller actually heard (after any substitution).
    delivered_violations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Conversation:
    state: ConversationState
    messages: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    """One Agent per process; one Conversation per session_id."""

    def __init__(
        self,
        db: Database,
        llm: LLMClient,
        *,
        location_id: str,
        guardrail_enabled: bool = True,
    ) -> None:
        self.db = db
        self.llm = llm
        self.location_id = location_id
        # False only in the eval harness, to measure what the guardrail would have caught.
        self.guardrail_enabled = guardrail_enabled
        self.conversations: dict[str, Conversation] = {}

    # ---- public -------------------------------------------------------------------

    def get_or_create(self, session_id: str, channel: str = "browser") -> Conversation:
        conv = self.conversations.get(session_id)
        if conv is None:
            state = ConversationState(
                call_id=session_id or str(uuid.uuid4()),
                channel=channel,
                location_id=self.location_id,
            )  # type: ignore[arg-type]
            conv = Conversation(state=state)
            self.conversations[session_id] = conv
            with self.db.transaction(immediate=True) as conn:
                events.record(
                    conn,
                    "call_started",
                    {},
                    location_id=self.location_id,
                    channel=channel,
                    call_id=state.call_id,
                )
        return conv

    def end(self, session_id: str) -> None:
        conv = self.conversations.pop(session_id, None)
        if conv is not None:
            with self.db.transaction(immediate=True) as conn:
                events.record(
                    conn,
                    "call_ended",
                    {"turns": conv.state.turns},
                    location_id=self.location_id,
                    channel=conv.state.channel,
                    call_id=conv.state.call_id,
                )

    def handle_message(self, session_id: str, text: str, channel: str = "browser") -> TurnResult:
        """One caller utterance in, one validated reply out."""
        conv = self.get_or_create(session_id, channel)
        state = conv.state
        bind_call(state.call_id, state.channel)
        try:
            return self._turn(conv, text)
        finally:
            clear_call()

    # ---- the loop ---------------------------------------------------------------------

    def _turn(self, conv: Conversation, text: str) -> TurnResult:
        state = conv.state
        state.turns += 1
        self._absorb_customer_text(state, text)
        conv.messages.append({"role": "user", "content": text})

        tool_results: list[ToolResult] = []
        usage = Usage()
        final_text = ""
        retried = False
        validation = ValidationResult()

        for attempt in range(config.GUARDRAIL_MAX_RETRIES + 1):
            # Recording and replay clients align their scripts on (turn, attempt).
            begin = getattr(self.llm, "begin_attempt", None)
            if begin is not None:
                begin(state.turns, attempt)
            try:
                final_text, attempt_results, attempt_usage = self._generate(conv, tool_results)
            except LLMUnavailableError as exc:
                log.warning("llm_unavailable", error=str(exc))
                return self._deliver(
                    conv, config.SAFE_FALLBACK_TEXT, tool_results, [], 0, usage, retried, None
                )
            tool_results = attempt_results
            usage.add(attempt_usage)

            validation = validate_response(final_text, tool_results, state)
            self._log_violations(state, validation, final_text)
            if not self.guardrail_enabled or validation.ok:
                break
            if validation.tier == 2:
                # Never retried. A regenerated answer that happens to pass is still a
                # model guessing about anaphylaxis.
                break
            if attempt < config.GUARDRAIL_MAX_RETRIES:
                retried = True
                state.guardrail_retries += 1
                conv.messages.append({"role": "user", "content": self._correction(validation)})

        if not self.guardrail_enabled or validation.ok:
            delivered = final_text
        elif validation.tier == 2:
            delivered = config.ALLERGY_ESCALATION_TEXT
            self._tier2_alert(state, validation, final_text)
        else:
            delivered = config.SAFE_FALLBACK_TEXT

        return self._deliver(
            conv,
            delivered,
            tool_results,
            validation.violations,
            validation.tier,
            usage,
            retried,
            final_text,
        )

    def _generate(
        self, conv: Conversation, prior_results: list[ToolResult]
    ) -> tuple[str, list[ToolResult], Usage]:
        """Run the model until it stops asking for tools. Returns (text, results, usage)."""
        state = conv.state
        results: list[ToolResult] = list(prior_results)
        usage = Usage()
        ctx = ToolContext(
            location_id=self.location_id, call_id=state.call_id, channel=state.channel
        )
        for _ in range(config.AGENT_MAX_TOOL_LOOPS):
            response = self.llm.complete(
                system_static=SYSTEM_PROMPT_STATIC,
                system_dynamic=self._dynamic_system(),
                messages=conv.messages,
                tools=TOOL_SCHEMAS,
            )
            usage.add(response.usage)
            conv.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                return config.SAFE_FALLBACK_TEXT, results, usage
            tool_uses = response.tool_uses
            if not tool_uses:
                return response.text, results, usage
            blocks = []
            for use in tool_uses:
                result = dispatch(self.db, ctx, use["name"], dict(use.get("input") or {}))
                results.append(result)
                self._absorb_tool_result(state, result)
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use["id"],
                        "content": result_to_content(result),
                        "is_error": not result.ok,
                    }
                )
            conv.messages.append({"role": "user", "content": blocks})
        log.warning("tool_loop_exhausted", call_id=state.call_id)
        return config.SAFE_FALLBACK_TEXT, results, usage

    # ---- state updates ---------------------------------------------------------------------

    def _absorb_customer_text(self, state: ConversationState, text: str) -> None:
        state.customer_numbers = customer_numbers(text)
        assessment = assess_request(text, direct_answer=False)
        if assessment.severity == "allergy":
            state.declared_dietary = text
            state.dietary_severity = "allergy"
        elif (
            assessment.severity == "preference"
            and state.dietary_severity == "none"
            and assessment.matched
        ):
            state.declared_dietary = text
            state.dietary_severity = "preference"

    def _absorb_tool_result(self, state: ConversationState, result: ToolResult) -> None:
        out = result.output
        if result.name == "check_availability":
            if out.get("status") == "known":
                state.last_known_quantities[str(out["item_id"])] = int(out["quantity"])
                state.availability_known = True
            elif out.get("status") == "unknown":
                state.availability_known = False
        elif result.name == "create_hold":
            note = result.input.get("dietary_note")
            if note and assess_request(str(note), direct_answer=True).severity == "allergy":
                state.declared_dietary = str(note)
                state.dietary_severity = "allergy"
            if out.get("ok") and out.get("hold_id") is not None:
                state.hold_ids.append(int(out["hold_id"]))
        elif result.name == "escalate_to_staff" and out.get("ok"):
            state.escalated = True

    # ---- plumbing --------------------------------------------------------------------------------

    def _dynamic_system(self) -> str:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT timezone FROM locations WHERE id = ?", (self.location_id,)
            ).fetchone()
        tz = ZoneInfo(row["timezone"]) if row else clock.UTC
        local: datetime = clock.now().astimezone(tz)
        return f"Current store time: {local.strftime('%A %-d %B %Y, %-I:%M %p')} ({tz.key})."

    @staticmethod
    def _correction(validation: ValidationResult) -> str:
        problems = "; ".join(f"{v.rule}: {v.excerpt!r}" for v in validation.violations)
        return (
            "[Operator note, not from the caller] Your last reply failed validation: "
            f"{problems}. Rewrite it. Only state quantities that check_availability returned this "
            "turn, only confirm a hold that create_hold accepted, and if you are not certain, "
            "say so "
            "and offer a callback."
        )

    def _log_violations(
        self, state: ConversationState, validation: ValidationResult, text: str
    ) -> None:
        if validation.ok:
            return
        with self.db.transaction(immediate=True) as conn:
            for v in validation.violations:
                events.record(
                    conn,
                    "guardrail_violation",
                    {
                        "rule": v.rule,
                        "tier": v.tier,
                        "excerpt": v.excerpt,
                        "enforced": self.guardrail_enabled,
                    },
                    location_id=self.location_id,
                    channel=state.channel,
                    call_id=state.call_id,
                )

    def _tier2_alert(
        self, state: ConversationState, validation: ValidationResult, text: str
    ) -> None:
        state.escalated = True
        with self.db.transaction(immediate=True) as conn:
            fan_out(
                conn,
                self.location_id,
                "guardrail_tier2",
                "urgent",
                "Assistant blocked for an allergen safety claim; call the customer back",
                {
                    "call_id": state.call_id,
                    "blocked_text": text,
                    "declared_dietary": state.declared_dietary,
                    "rules": [v.rule for v in validation.violations if v.tier == 2],
                },
            )

    def _deliver(
        self, conv, delivered, tool_results, violations, tier, usage, retried, raw_text
    ) -> TurnResult:
        if raw_text is None or delivered == raw_text:
            delivered_violations = list(violations)
        else:
            delivered_violations = validate_response(delivered, tool_results, conv.state).violations
        # History must hold what the caller actually heard, not the blocked text, or the
        # model builds its next turn on a claim nobody received.
        if conv.messages and conv.messages[-1]["role"] == "assistant":
            kept = [b for b in conv.messages[-1]["content"] if b.get("type") != "text"]
            conv.messages[-1]["content"] = [*kept, {"type": "text", "text": delivered}]
        else:
            conv.messages.append(
                {"role": "assistant", "content": [{"type": "text", "text": delivered}]}
            )
        return TurnResult(
            reply=delivered,
            tool_calls=[r.to_dict() for r in tool_results],
            violations=[v.to_dict() for v in violations],
            blocked_tier=tier if self.guardrail_enabled and violations else 0,
            escalated=conv.state.escalated,
            retried=retried,
            usage=usage,
            raw_text=raw_text,
            delivered_violations=[v.to_dict() for v in delivered_violations],
        )
