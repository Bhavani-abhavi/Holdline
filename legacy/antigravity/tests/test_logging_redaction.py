"""No phone number may reach a log line. Enforced by a processor, not a convention."""

from __future__ import annotations

from holdline.logging_setup import _redact_pii, hash_phone, redact_phone


def test_a_redacted_number_keeps_four_digits_and_hides_the_rest() -> None:
    """Staff need to recognise which call a line belongs to; four digits identify nobody."""
    redacted = redact_phone("+1 (312) 555-0188")
    visible_digits = redacted.split("/h:")[0]

    assert visible_digits == "***0188"
    assert "3125550188" not in redacted
    assert "555" not in visible_digits


def test_the_same_number_always_produces_the_same_fingerprint() -> None:
    """Stability is what makes a repeat caller traceable across log lines."""
    assert hash_phone("+1 (312) 555-0188") == hash_phone("3125550188")


def test_different_numbers_produce_different_fingerprints() -> None:
    """A fingerprint that collided routinely would be useless for tracing."""
    assert hash_phone("3125550188") != hash_phone("3125550189")


def test_an_empty_number_is_handled_without_raising() -> None:
    """Holds may have no phone number at all, and logging must not care."""
    assert redact_phone("") == "***"
    assert hash_phone("") == "none"


def test_the_processor_redacts_phone_shaped_keys_automatically() -> None:
    """A developer who logs customer_phone by habit still cannot leak it."""
    event = {"event": "hold_created", "customer_phone": "312-555-0188", "quantity": 1}

    processed = _redact_pii(None, "info", event)

    assert "0188" in processed["customer_phone"]
    assert "312-555-0188" not in str(processed)
    assert processed["quantity"] == 1


def test_the_processor_scrubs_numbers_embedded_in_free_text() -> None:
    """Summaries are written by humans and end up quoting numbers they should not."""
    event = {"event": "alert", "summary": "Call back Priya on +1 312 555 0188 about the cake"}

    processed = _redact_pii(None, "info", event)

    assert "555 0188" not in processed["summary"]
    assert "0188" in processed["summary"]


def test_prices_and_quantities_are_left_alone() -> None:
    """Over-eager scrubbing would destroy the log lines the glass box depends on.

    Only ten-or-more-digit runs look like phone numbers. A price, a hold id and a
    pickup time must survive untouched.
    """
    event = {"event": "hold_created", "summary": "Hold 42 for 2 units at $42.00, pickup 5:45pm"}

    processed = _redact_pii(None, "info", event)

    assert processed["summary"] == event["summary"]


def test_non_string_values_pass_through_untouched() -> None:
    """The processor runs on every line, so it must not choke on structured payloads."""
    event = {"event": "tool_call", "arguments": {"quantity": 2}, "ok": True, "age": None}

    processed = _redact_pii(None, "info", dict(event))

    assert processed == event
