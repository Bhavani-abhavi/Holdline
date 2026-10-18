"""All tunable constants and environment configuration in one place.

Every threshold carries a comment on how it was chosen. Nothing elsewhere in the
code base holds a magic number. Environment variables are read once at import
time through `Settings`; `validate()` fails loudly on a missing required key so
a misconfiguration surfaces at startup, never halfway through a phone call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Domain thresholds -----------------------------------------------------------

# POS data older than this is treated as unknown. 10 minutes: a busy bakery sells a
# cake every few minutes, so a 10-minute-old count can already be wrong by several
# units. Beyond it the assistant refuses to state ANY number (availability.py).
STALE_THRESHOLD_SECONDS = 600

# Grace window past the stated pickup time before a hold is released. 45 minutes
# covers normal lateness without tying up stock for the evening.
DEFAULT_HOLD_MINUTES = 45

# Above this quantity a human decides. 2 is the point where one call can empty a
# small bakery's shelf of an item.
MAX_AUTO_APPROVE_QTY = 2

# Holds further out than this need a human: beyond ~4 hours the bakery may want to
# bake to order rather than reserve shelf stock.
MAX_HOLD_HOURS_AHEAD = 4

# Triggers the "big order" conversation path in the system prompt.
LARGE_ORDER_QTY = 6

# Tier 1 guardrail violations are retried this many times before the safe fallback.
# Tier 2 NEVER retries; see guardrails.py for why this is not configurable.
GUARDRAIL_MAX_RETRIES = 1

# Runaway protection for the tool loop. A normal turn uses 1-3 tool calls.
AGENT_MAX_TOOL_LOOPS = 5

# Every external call has a timeout. 20 seconds is long enough for a tool-use turn
# and short enough that a caller is not left in silence.
LLM_TIMEOUT_SECONDS = 20.0

# Phone replies are short; capping output keeps cost down (output tokens cost 5x input).
LLM_MAX_TOKENS = 150

# Do not make this configurable to False. See dietary.py and docs/adr/0001.
ALLERGY_ALWAYS_ESCALATE = True

# The expiry worker wakes this often. Holds expire on a 45-minute grace window, so
# 15-second granularity is more than enough.
EXPIRY_WORKER_INTERVAL_SECONDS = 15

# Staff pages poll this often (milliseconds). 2 seconds feels live without hammering SQLite.
STAFF_POLL_INTERVAL_MS = 2000

# Hash prefix length for phone numbers in logs. 10 hex chars is enough to correlate
# a caller within one day's logs and useless for recovering the number.
PHONE_HASH_CHARS = 10

# Scripts the guardrail substitutes for a blocked response. Kept here so tests
# and the eval harness can assert on them exactly.
SAFE_FALLBACK_TEXT = (
    "Let me double-check that with someone here and call you right back. "
    "What's the best number to reach you on?"
)
ALLERGY_ESCALATION_TEXT = (
    "I don't want to guess on an allergy over an automated line. I'm putting a soft "
    "hold on that and having someone from the bakery call you back within a few "
    "minutes to confirm. What's the best number for you?"
)


# --- Environment-backed settings ----------------------------------------------------


@dataclass
class Settings:
    """Environment-backed settings. Read once; pass the instance around."""

    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    model: str = field(default_factory=lambda: os.getenv("HOLDLINE_MODEL", "claude-sonnet-5-5"))
    smoke_model: str = field(
        default_factory=lambda: os.getenv("HOLDLINE_SMOKE_MODEL", "claude-haiku-5-5")
    )
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("HOLDLINE_DB_PATH", "holdline.db"))
    )
    agent_impl: str = field(default_factory=lambda: os.getenv("HOLDLINE_AGENT", "manual"))
    llm_backend: str = field(default_factory=lambda: os.getenv("HOLDLINE_LLM", "anthropic"))
    log_level: str = field(default_factory=lambda: os.getenv("HOLDLINE_LOG_LEVEL", "INFO"))

    def validate(self, *, need_llm: bool) -> None:
        """Raise ConfigError on a missing required value.

        Args:
            need_llm: True when the process will call the model. Tests and the
                replay stage of the eval harness pass False.
        """
        from holdline.exceptions import ConfigError  # local import avoids a cycle

        if self.agent_impl not in {"manual", "graph"}:
            raise ConfigError(
                f"HOLDLINE_AGENT must be 'manual' or 'graph', got {self.agent_impl!r}"
            )
        if self.llm_backend not in {"anthropic", "fake"}:
            raise ConfigError(
                f"HOLDLINE_LLM must be 'anthropic' or 'fake', got {self.llm_backend!r}"
            )
        if need_llm and self.llm_backend == "anthropic" and not self.anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example) or set "
                "HOLDLINE_LLM=fake to run without a model."
            )


def get_settings() -> Settings:
    """Build a fresh Settings from the environment (cheap; no caching needed)."""
    return Settings()
