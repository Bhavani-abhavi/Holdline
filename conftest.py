"""Shared fixtures.

Every test runs against its own temporary database file rather than a shared
in-memory one. Two reasons, both about Phase 2: `:memory:` databases are not
shared between connections, and the concurrency test needs several real
connections to the same file. Building the fixture that way from the start means
the concurrency test does not need a special case.
"""

from __future__ import annotations

import os

# Set before importing anything from holdline: config reads the log level at
# import time, and structlog is configured once per process. At INFO, the
# concurrency test emits several thousand lines that bury a real failure.
os.environ.setdefault("HOLDLINE_LOG_LEVEL", "WARNING")

from collections.abc import Iterator
from pathlib import Path

import pytest

from holdline import config, db
from holdline.seed import seed


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the application at an empty database for the duration of one test.

    Autouse, so no test can accidentally read or write the developer's real
    `data/holdline.db`.

    Yields:
        The path to the temporary database file.
    """
    target = tmp_path / "holdline_test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", target)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    # Drop any connection left over from a previous test; it points at a file
    # that no longer exists.
    db.close_connection()
    db.initialize(target)

    yield target

    db.close_connection()


@pytest.fixture
def seeded_db(temp_db: Path) -> Path:
    """A temporary database populated with the demo location and catalogue."""
    seed()
    return temp_db
