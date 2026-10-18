"""Staff notification adapters.

One protocol, three implementations. `fan_out` sends to every configured
notifier and isolates each so one failure does not block the others. Swapping
in real SMS later is one class implementing one method; see docs/adr/0005.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Protocol

from holdline import clock
from holdline.logging_setup import get_logger, phone_fingerprint

log = get_logger(__name__)


class Notifier(Protocol):
    """Anything that can deliver a staff alert."""

    def send(
        self,
        conn: sqlite3.Connection,
        location_id: str,
        kind: str,
        urgency: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None: ...


class ConsoleNotifier:
    """Structured log line. Always on during development. Never logs a phone number."""

    def send(
        self,
        conn: sqlite3.Connection,
        location_id: str,
        kind: str,
        urgency: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        safe = {k: v for k, v in payload.items() if k != "customer_phone"}
        safe["phone_fp"] = phone_fingerprint(payload.get("customer_phone"))
        log.info(
            "staff_alert",
            kind=kind,
            urgency=urgency,
            summary=summary,
            location_id=location_id,
            **safe,
        )


class InAppNotifier:
    """Writes staff_alerts; the staff page polls it. Takes the caller's connection so
    the alert commits atomically with the hold that caused it."""

    def send(
        self,
        conn: sqlite3.Connection,
        location_id: str,
        kind: str,
        urgency: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO staff_alerts (location_id, at, kind, urgency, summary, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (location_id, clock.iso(), kind, urgency, summary, json.dumps(payload, sort_keys=True)),
        )


class SmsNotifier:
    """Documented stub. A real implementation would call Twilio's Messages API with
    the on-duty manager's number from a staff roster table. Not built in v1."""

    def send(
        self,
        conn: sqlite3.Connection,
        location_id: str,
        kind: str,
        urgency: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        raise NotImplementedError("SMS delivery is a documented stub in v1")


DEFAULT_NOTIFIERS: tuple[Notifier, ...] = (ConsoleNotifier(), InAppNotifier())


def fan_out(
    conn: sqlite3.Connection,
    location_id: str,
    kind: str,
    urgency: str,
    summary: str,
    payload: dict[str, Any],
    notifiers: tuple[Notifier, ...] = DEFAULT_NOTIFIERS,
) -> None:
    """Deliver to every notifier. A failing notifier is logged and skipped so a
    broken SMS gateway never stops the in-app alert."""
    for notifier in notifiers:
        try:
            notifier.send(conn, location_id, kind, urgency, summary, payload)
        except NotImplementedError:
            log.debug("notifier_stub_skipped", notifier=type(notifier).__name__)
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.error("notifier_failed", notifier=type(notifier).__name__, error=str(exc))


def open_alerts(conn: sqlite3.Connection, location_id: str | None = None) -> list[dict[str, Any]]:
    """Unacknowledged alerts, oldest first."""
    if location_id:
        rows = conn.execute(
            "SELECT * FROM staff_alerts WHERE acknowledged = 0 AND location_id = ? ORDER BY at, id",
            (location_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM staff_alerts WHERE acknowledged = 0 ORDER BY at, id"
        ).fetchall()
    out = [dict(r) for r in rows]
    for row in out:
        row["payload"] = json.loads(row["payload"])
    return out


def acknowledge(conn: sqlite3.Connection, alert_id: int) -> bool:
    """Mark one alert handled. Returns False if it did not exist."""
    cur = conn.execute("UPDATE staff_alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    return cur.rowcount == 1
