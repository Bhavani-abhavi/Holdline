"""Routes: chat, dashboard, staff actions, demo controls, the voice front door, health."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from holdline import clock
from holdline.config import Settings
from holdline.llm import FakeLLM, text_response, tool_response
from holdline.main import create_app
from tests.conftest import FROZEN_NOW


@pytest.fixture
def client(tmp_path: Path):
    clock.freeze(FROZEN_NOW)
    llm = FakeLLM()
    settings = Settings(db_path=tmp_path / "api.db", llm_backend="fake")
    app = create_app(db=None, llm=llm, settings=settings)
    with TestClient(app) as c:
        c.llm = llm  # type: ignore[attr-defined]
        yield c
    clock.unfreeze()


def test_pages_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/staff").status_code == 200
    assert client.get("/analytics").status_code == 200
    assert "text/css" in client.get("/static/style.css").headers["content-type"]


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["expiry_worker"] is True
    assert body["last_pos_sync_age_seconds"] == 0


def test_chat_returns_tool_calls_and_glass_box_sees_them(client):
    client.llm.queue(
        tool_response(("check_availability", {"item_query": "chocolate cake"})),
        text_response(
            "Automated assistant here. We've got 10 right now. Want me to set one aside?"
        ),
    )
    body = client.post("/api/chat", json={"session_id": "s1", "message": "chocolate cake?"}).json()
    assert body["tool_calls"][0]["result"]["quantity"] == 10
    assert body["blocked_tier"] == 0
    dash = client.get("/api/dashboard").json()
    kinds = [e["kind"] for e in dash["events"]]
    assert "tool_call" in kinds and "call_started" in kinds
    assert dash["stale"] is False
    assert any(s["item_id"] == "cake_choc_9" and s["sellable"] == 10 for s in dash["stock"])


def test_dashboard_after_id_is_incremental(client):
    first = client.get("/api/dashboard").json()
    last_id = first["events"][-1]["id"]
    assert client.get(f"/api/dashboard?after_id={last_id}").json()["events"] == []


def test_delivery_raises_sellable_and_counters(client):
    r = client.post(
        "/api/staff/delivery", json={"item_id": "cake_carrot", "quantity": 10, "note": "van"}
    )
    assert r.json()["ok"]
    dash = client.get("/api/dashboard").json()
    carrot = next(s for s in dash["stock"] if s["item_id"] == "cake_carrot")
    assert carrot["sellable"] == 4 + 10 - 1
    assert dash["counters"]["cake_carrot"]["received"] == 10
    assert (
        client.post("/api/staff/delivery", json={"item_id": "nope", "quantity": 1}).status_code
        == 404
    )


def test_voice_tool_front_door_shares_the_ledger(client):
    r = client.post(
        "/api/voice/tool",
        json={
            "tool_name": "create_hold",
            "call_id": "phone-1",
            "arguments": {"item_id": "cake_choc_9", "quantity": 3, "pickup_time": "in 1 hour"},
        },
    ).json()
    assert r["result"]["status"] == "pending_approval"
    hold_id = r["result"]["hold_id"]
    dash = client.get("/api/dashboard").json()
    assert any(h["id"] == hold_id and h["channel"] == "phone" for h in dash["holds"])
    assert any(e["kind"] == "tool_call" and e["channel"] == "phone" for e in dash["events"])
    alerts = client.get("/api/staff/alerts").json()["alerts"]
    assert any(a["kind"] == "approval_needed" for a in alerts)
    # Approve from the staff page, then it is active and collectable.
    assert client.post(f"/api/staff/holds/{hold_id}/approve").json()["ok"]
    assert client.post(f"/api/staff/holds/{hold_id}/approve").status_code == 409
    assert client.post(f"/api/staff/holds/{hold_id}/collect").json()["ok"]
    assert (
        client.post("/api/voice/tool", json={"tool_name": "nope", "call_id": "x"}).status_code
        == 400
    )


def test_alert_ack_and_decline(client):
    r = client.post(
        "/api/voice/tool",
        json={
            "tool_name": "create_hold",
            "call_id": "phone-2",
            "arguments": {"item_id": "cake_choc_9", "quantity": 5, "pickup_time": "in 1 hour"},
        },
    ).json()
    alert_id = client.get("/api/staff/alerts").json()["alerts"][0]["id"]
    assert client.post(f"/api/staff/alerts/{alert_id}/ack").json()["ok"]
    assert client.get("/api/staff/alerts").json()["alerts"] == []
    assert client.post(f"/api/staff/holds/{r['result']['hold_id']}/decline").json()["ok"]


def test_demo_controls(client):
    assert client.post("/api/simulate/stale").json()["ok"]
    dash = client.get("/api/dashboard").json()
    assert dash["stale"] is True and all(s["sellable"] is None for s in dash["stock"])
    assert client.post("/api/simulate/resync").json()["ok"]
    assert client.get("/api/dashboard").json()["stale"] is False
    sold = client.post("/api/simulate/sales", json={"minutes": 10}).json()["sold"]
    assert sold == 5
    assert client.post("/api/reset").json()["ok"]
    assert client.get("/api/dashboard").json()["counters"]["cake_choc_9"]["sold"] == 0


def test_voice_events_logged_as_phone(client):
    assert client.post("/api/voice/events", json={"call_id": "p9", "event": "call_started"}).json()[
        "ok"
    ]
    dash = client.get("/api/dashboard").json()
    assert any(e["kind"] == "call_started" and e["channel"] == "phone" for e in dash["events"])


def test_menu_has_prices_not_quantities(client):
    items = client.get("/api/menu").json()["items"]
    assert len(items) == 12 and all("$" in i["price"] for i in items)
