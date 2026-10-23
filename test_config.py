"""Configuration must fail loudly at startup, and must never leak a secret."""

from __future__ import annotations

import importlib

import pytest

from holdline import config
from holdline.exceptions import ConfigurationError


def test_core_startup_needs_no_credentials() -> None:
    """Phases 1 and 2 must run on a clean clone with no .env at all."""
    config.validate_startup(("core",))


def test_missing_model_key_fails_before_serving_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing model credential is a boot failure, not a mid-call surprise."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigurationError) as caught:
        config.validate_startup(("agent",))

    assert "ANTHROPIC_API_KEY" in str(caught.value)


def test_error_message_names_missing_keys_but_never_echoes_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation errors get logged and pasted into tickets, so they carry no secrets.

    A credential that *is* present sits in the environment while a different
    required key is missing. The resulting message must name the missing key and
    say nothing whatsoever about the value of the one that was found.
    """
    secret = "sk-ant-this-must-never-appear-anywhere"  # noqa: S105 -- fake test value
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.delenv("VAPI_API_KEY", raising=False)
    monkeypatch.setitem(
        config.REQUIRED_KEYS_BY_FEATURE,
        "agent",
        ("ANTHROPIC_API_KEY", "VAPI_API_KEY"),
    )

    with pytest.raises(ConfigurationError) as caught:
        config.validate_startup(("agent",))

    message = str(caught.value)
    assert "VAPI_API_KEY" in message
    assert secret not in message


def test_unknown_feature_name_is_rejected() -> None:
    """A typo'd feature name must not silently validate nothing."""
    with pytest.raises(ConfigurationError, match="unknown feature"):
        config.validate_startup(("agnet",))


def test_non_numeric_threshold_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd threshold must fail, not quietly revert to the default."""
    monkeypatch.setenv("STALE_THRESHOLD_SECONDS", "ten minutes")

    with pytest.raises(ConfigurationError, match="must be an integer"):
        config._env_int("STALE_THRESHOLD_SECONDS", 600)


def test_large_order_threshold_must_exceed_the_auto_approve_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contradictory thresholds are caught at boot, not discovered by a caller.

    If LARGE_ORDER_QTY were not above MAX_AUTO_APPROVE_QTY, the large-order
    conversation path would be unreachable and nobody would notice.
    """
    monkeypatch.setattr(config, "LARGE_ORDER_QTY", 2)
    monkeypatch.setattr(config, "MAX_AUTO_APPROVE_QTY", 2)

    with pytest.raises(ConfigurationError, match="LARGE_ORDER_QTY"):
        config.validate_startup(("core",))


def test_allergy_escalation_cannot_be_switched_off_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one rule with no off switch must have no off switch.

    Proven by setting every plausible environment variable name to a false value
    and reloading the module: the constant is read from nowhere, so it stays True.
    """
    for name in ("ALLERGY_ALWAYS_ESCALATE", "HOLDLINE_ALLERGY_ALWAYS_ESCALATE"):
        monkeypatch.setenv(name, "false")

    try:
        reloaded = importlib.reload(config)
        assert reloaded.ALLERGY_ALWAYS_ESCALATE is True
    finally:
        # Restore the module for the rest of the session; the fixture's
        # monkeypatched paths are reapplied per test, so this is safe.
        importlib.reload(config)
