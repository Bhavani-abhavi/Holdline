"""Append-only event log.

Every business fact (sale, delivery, hold, tool call, guardrail violation) is
written here once and never changed. The glass box on the customer page and
the Phase 8 data platform both read this table; neither reads the chat
response. That is why phone conversations show up in the browser for free.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from holdline import clock


def record(
    conn: sqlite3.Connection,
    kind: str,
    detail: dict[str, Any],
    *,
    location_id: str | None = None,
    channel: str | None = None,
    call_id: str | None = None,
    trace_id: str | None = None,
) -> int:
    """Insert one event on an existing connection and return its id.

    Takes a connection rather than a Database so callers inside a transaction
    log atomically with the write they describe.
    """
    cur = conn.execute(
        "INSERT INTO events (at, location_id, kind, channel, call_id, trace_id, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            clock.iso(),
            location_id,
            kind,
            channel,
            call_id,
            trace_id,
            json.dumps(detail, sort_keys=True),
        ),
    )
    return int(cur.lastrowid or 0)


def recent(conn: sqlite3.Connection, *, limit: int = 50, after_id: int = 0) -> list[dict[str, Any]]:
    """Most recent events, newest last, optionally only those after a given id."""
    rows = conn.execute(
        "SELECT id, at, location_id, kind, channel, call_id, trace_id, detail FROM events"
        " WHERE id > ? ORDER BY id DESC LIMIT ?",
        (after_id, limit),
    ).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for row in out:
        row["detail"] = json.loads(row["detail"])
    return out
