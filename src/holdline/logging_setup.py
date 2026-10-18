"""structlog configuration and PII helpers.

Rules enforced here:
  * JSON lines to stdout; no print() anywhere outside hand-run scripts.
  * Every log line carries call_id when one is bound (`bind_call`).
  * Phone numbers never appear in logs: use `phone_fingerprint()`.
"""

from __future__ import annotations

import hashlib
import logging

import structlog

from holdline import config


def configure(level: str = "INFO") -> None:
    """Configure structlog once. Safe to call repeatedly."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Named logger; `name` is the module path."""
    return structlog.get_logger(name)


def bind_call(call_id: str | None, channel: str | None = None) -> None:
    """Attach call_id (and channel) to every log line until `clear_call()`."""
    structlog.contextvars.clear_contextvars()
    if call_id:
        structlog.contextvars.bind_contextvars(call_id=call_id)
    if channel:
        structlog.contextvars.bind_contextvars(channel=channel)


def clear_call() -> None:
    """Drop the per-call context."""
    structlog.contextvars.clear_contextvars()


def phone_fingerprint(phone: str | None) -> str | None:
    """Short, non-reversible fingerprint of a phone number for log correlation."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return hashlib.sha256(digits.encode()).hexdigest()[: config.PHONE_HASH_CHARS]
