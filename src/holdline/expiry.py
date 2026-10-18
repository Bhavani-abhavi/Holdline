"""Background worker that releases holds past their grace window.

Runs `holds.expire_due_holds` every EXPIRY_WORKER_INTERVAL_SECONDS. The work
itself is idempotent, so the worker can be restarted or run alongside a manual
call without double-releasing anything.
"""

from __future__ import annotations

import asyncio
import sqlite3

from holdline import config
from holdline.db import Database
from holdline.holds import expire_due_holds
from holdline.logging_setup import get_logger

log = get_logger(__name__)


async def run_forever(db: Database, stop: asyncio.Event) -> None:
    """Loop until `stop` is set. Errors are logged, never fatal: a transient SQLite
    lock must not kill the worker for the rest of the day."""
    log.info("expiry_worker_started", interval_s=config.EXPIRY_WORKER_INTERVAL_SECONDS)
    while not stop.is_set():
        try:
            await asyncio.to_thread(expire_due_holds, db)
        except sqlite3.Error as exc:
            log.warning("expiry_worker_error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.EXPIRY_WORKER_INTERVAL_SECONDS)
        except TimeoutError:
            continue
    log.info("expiry_worker_stopped")
