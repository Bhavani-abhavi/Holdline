"""The health endpoint separates "the process is broken" from "the data is stale"."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from holdline import config, db
from holdline.main import app
from holdline.timeutils import to_iso, utc_now


def _client() -> TestClient:
    """Return a test client. The app's lifespan runs on context entry."""
    return TestClient(app)


def test_health_reports_fresh_data_after_seeding(seeded_db: Path) -> None:
    """A freshly seeded database is healthy and its POS data is current."""
    with _client() as client:
        body = client.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["items"] == 12
    assert body["checks"]["pos_data_fresh"] is True
    assert body["checks"]["pos_sync_age_seconds"] < 60


def test_stale_pos_data_still_returns_two_hundred(seeded_db: Path) -> None:
    """Stale data is a data problem, not a liveness problem.

    Returning a failure here would make an orchestrator restart a server that has
    nothing wrong with it. The staleness is reported as a field so a monitor can
    decide for itself what to do about it.
    """
    stale_moment = utc_now().replace(year=utc_now().year - 1)
    with db.transaction(immediate=True) as conn:
        conn.execute("UPDATE stock SET last_synced_at = ?", (to_iso(stale_moment),))

    with _client() as client:
        response = client.get("/healthz")
    body = response.json()

    assert response.status_code == 200
    assert body["checks"]["pos_data_fresh"] is False
    assert body["checks"]["pos_sync_age_seconds"] > config.STALE_THRESHOLD_SECONDS


def test_health_reports_the_oldest_sync_not_the_newest(seeded_db: Path) -> None:
    """Health is the worst case across the catalogue.

    One item that stopped syncing an hour ago is a real problem even while eleven
    others are current, so a single stale row must drag the reported age up.
    """
    stale_moment = utc_now().replace(year=utc_now().year - 1)
    with db.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE stock SET last_synced_at = ? WHERE item_id = ?",
            (to_iso(stale_moment), "drink_cold_brew"),
        )

    with _client() as client:
        body = client.get("/healthz").json()

    assert body["checks"]["pos_data_fresh"] is False


def test_health_on_an_empty_database_reports_no_sync_rather_than_guessing(
    temp_db: Path,
) -> None:
    """With no stock at all there is no freshness to report, and None says so."""
    with _client() as client:
        body = client.get("/healthz").json()

    assert body["checks"]["items"] == 0
    assert body["checks"]["pos_sync_age_seconds"] is None
    assert body["checks"]["pos_data_fresh"] is False
