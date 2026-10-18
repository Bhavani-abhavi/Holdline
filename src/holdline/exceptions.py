"""Custom exception types. Catch these, never bare `except:`."""


class HoldLineError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(HoldLineError):
    """A required setting is missing or invalid. Raised at startup, never mid-call."""


class StaleInventoryError(HoldLineError):
    """The POS sync is older than STALE_THRESHOLD_SECONDS; no number may be stated."""


class InsufficientStockError(HoldLineError):
    """A hold was requested for more units than are sellable."""


class GuardrailViolation(HoldLineError):
    """A generated response failed validation. Carries the tier and the rule."""

    def __init__(self, tier: int, rule: str, excerpt: str) -> None:
        super().__init__(f"tier {tier} {rule}: {excerpt!r}")
        self.tier = tier
        self.rule = rule
        self.excerpt = excerpt


class ApprovalRequired(HoldLineError):
    """A hold is outside auto-approval policy and waits for staff."""


class UnknownItemError(HoldLineError):
    """No menu item matched the caller's words."""


class LLMUnavailableError(HoldLineError):
    """The model call failed or timed out. The caller gets the safe fallback."""
