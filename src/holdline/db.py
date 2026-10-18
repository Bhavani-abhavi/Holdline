"""SQLite connection helper and transaction context manager.

Design:
  * `Database(path)` is the single handle the rest of the code passes around.
    Tests build one on a temp path; the app builds one from settings.
  * Every call opens a fresh connection and closes it. SQLite connections are
    cheap, and per-call connections make the code thread-safe without a pool:
    the concurrency test fires ten threads at `create_hold` and each thread gets
    its own connection.
  * `transaction(immediate=True)` issues BEGIN IMMEDIATE, which takes the write
    lock up front. That is what makes `holds.create_hold` atomic: the availability
    check and the insert happen under one lock, so two callers cannot both read
    "1 left" and both insert. WAL mode lets readers proceed while a writer holds
    that lock, which keeps the dashboard responsive.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

# How long a writer waits for the lock before SQLite raises "database is locked".
# Ten threads contending for one lock each finish in milliseconds, so 5 s is ample.
BUSY_TIMEOUT_SECONDS = 5.0


class Database:
    """A path plus the small set of operations every module needs."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        """Open a connection with the pragmas this application relies on."""
        conn = sqlite3.connect(
            self.path,
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # we manage transactions explicitly with BEGIN
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """A connection for reads or single autocommit statements."""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Run a block inside one transaction; commit on success, roll back on error.

        Args:
            immediate: use BEGIN IMMEDIATE to take the write lock at the start.
                Required whenever a read decides a write (check-then-insert).
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def apply_schema(self) -> None:
        """Create tables and indexes if they do not exist (idempotent)."""
        schema_sql = resources.files("holdline").joinpath("schema.sql").read_text()
        with self.connection() as conn:
            conn.executescript(schema_sql)

    def reset(self) -> None:
        """Drop every table. Used by the demo's reset button and by tests."""
        with self.connection() as conn:
            tables = [
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ]
            for table in tables:
                if not table.startswith("sqlite_"):
                    conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        self.apply_schema()
