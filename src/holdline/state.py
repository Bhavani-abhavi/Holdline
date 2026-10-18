"""Per-conversation state shared by the agent loop and the guardrail.

In the manual loop this is a plain dataclass the agent mutates between tool
calls. Phase 5 lifts the same fields into a LangGraph state schema; keeping
them here, explicit and typed, is what makes that port a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Channel = Literal["browser", "phone"]
Severity = Literal["none", "preference", "allergy"]


@dataclass
class ConversationState:
    """Everything the guardrail needs to judge a reply that is not in the reply."""

    call_id: str
    channel: Channel
    location_id: str

    # Grounding: what the tools have said so far.
    availability_known: bool = True  # False once any availability result was "unknown"
    last_known_quantities: dict[str, int] = field(default_factory=dict)  # item_id -> qty

    # Safety: what the caller has declared, across the whole call.
    declared_dietary: str | None = None
    dietary_severity: Severity = "none"

    # The caller's own numbers this turn. "two cakes" echoed back is not a claim.
    customer_numbers: set[int] = field(default_factory=set)

    # Transaction bookkeeping.
    hold_ids: list[int] = field(default_factory=list)

    # Control.
    guardrail_retries: int = 0
    escalated: bool = False
    turns: int = 0
