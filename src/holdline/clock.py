"""A single injectable source of time.

Every module asks `clock.now()` instead of `datetime.now()` so tests and the
eval replay stage can freeze time and get byte-identical tool results.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

_now_provider: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731


def now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return _now_provider()


def iso(dt: datetime | None = None) -> str:
    """ISO 8601 string (seconds precision, UTC) for storage."""
    return (dt or now()).astimezone(UTC).replace(microsecond=0).isoformat()


def parse(value: str) -> datetime:
    """Parse an ISO 8601 string back to an aware UTC datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def freeze(at: datetime) -> None:
    """Pin `now()` to a fixed instant. Tests call `unfreeze()` afterwards."""
    global _now_provider
    fixed = at if at.tzinfo else at.replace(tzinfo=UTC)
    _now_provider = lambda: fixed  # noqa: E731


def unfreeze() -> None:
    """Restore the real clock."""
    global _now_provider
    _now_provider = lambda: datetime.now(UTC)  # noqa: E731
