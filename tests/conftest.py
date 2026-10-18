"""Shared fixtures. Every test gets a fresh seeded database on a temp path."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from holdline import clock
from holdline.db import Database
from holdline.logging_setup import configure
from holdline.seed import LOCATION_ID, seed

configure("WARNING")

FROZEN_NOW = datetime(2026, 10, 18, 15, 0, 0, tzinfo=UTC)  # a Saturday, 10am Chicago


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    """Seeded database with a frozen clock, unfrozen afterwards."""
    clock.freeze(FROZEN_NOW)
    database = Database(tmp_path / "test.db")
    seed(database)
    yield database
    clock.unfreeze()


@pytest.fixture
def location_id() -> str:
    return LOCATION_ID
