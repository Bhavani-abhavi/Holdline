"""The HTTP layer: what the two pages actually consume.

These test the API surface rather than the domain beneath it, because the shapes the
browser reads are their own contract. Phase 3 shipped a bug that lived exactly here --
`Turn.as_dict()` reported no violations when there were some -- and it survived because
every test asserted on the objects rather than on the payload.

`llm.call_model` is replaced throughout, so nothing here costs money or needs a key.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from holdline import db, inventory, llm, main
from holdline.llm import ModelReply, ModelToolCall

from helpers import CAKE, LOCATION_ID, insert_hold, set_stock


def says(text: str) -> ModelReply:
    """A scripted reply that ends the turn."""
    return ModelReply(
        text=text, stop_reason="end_turn", raw_content=[{"type": "text", "text": text}]
    )


def calls(name: str, arguments: dict[str, Any]) -> ModelReply:
    """A scripted reply that asks for one tool."""
    return ModelReply(
        text="",
        tool_calls=(ModelToolCall(id="tu_1", name=name, arguments=arguments),),
        stop_reason="tool_use",
        raw_content=[{"type": "tool_use", "id": "tu_1", "name": name, "input": arguments}],
    )


class Script:
    """Stands in for the provider."""

    def __init__(self, *replies: ModelReply) -> None:
        """Queue the replies."""
        self.queue = list(replies)

    def __call__(self, messages: Any, *, system: str = "") -> ModelReply:
        """Return the next scripted reply."""
        return self.queue.pop(0) if self.queue else says("(script exhausted)")


@pytest.fixture
def client(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A test client with a seeded database and no live model.

    The expiry worker runs inside the app's lifespan, so entering the context manager
    exercises startup and shutdown too -- the paths a plain function call would skip.
    """
    monkeypatch.setattr(llm, "call_model", Script())
    main._SESSIONS.clear()
    with TestClient(main.app) as test_client:
        yield test_client
    main._SESSIONS.clear()


def script(monkeypatch: pytest.MonkeyPatch, *replies: ModelReply) -> None:
    """Install a scripted provider for one test."""
    monkeypatch.setattr(llm, "call_model", Script(*replies))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/staff", "/analytics"])
def test_every_page_is_served(client: TestClient, path: str) -> None:
    """A navigation link that 404s is worse than no link."""
    response = client.get(path)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_customer_page_always_offers_a_text_input(client: TestClient) -> None:
    """The demo has to survive a bad microphone, or a borrowed laptop in an interview."""
    body = client.get("/").text

    assert 'id="saytext"' in body
    assert 'id="sayform"' in body


def test_the_customer_page_warns_about_browser_support(client: TestClient) -> None:
    """Speech recognition is Chrome and Edge only, and that is detected, not assumed."""
    body = client.get("/").text

    assert "webkitSpeechRecognition" in body
    assert "browserwarn" in body


def test_voice_output_goes_through_a_single_function(client: TestClient) -> None:
    """One `speak()` so a better TTS provider is a one-function change."""
    body = client.get("/").text

    assert "function speak(" in body
    assert body.count("speechSynthesis.speak") == 1


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


def test_a_turn_returns_everything_the_glass_box_needs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload contract, asserted on the payload rather than on the objects."""
    script(
        monkeypatch,
        calls("check_availability", {"item_query": "chocolate cake"}),
        says("We have eleven right now."),
    )

    body = client.post("/api/chat", json={"session_id": "s1", "message": "How many?"}).json()

    assert body["reply"] == "We have eleven right now."
    assert body["action"] == "accept"
    assert body["guardrail_tier"] == 0
    assert body["violations"] == []
    assert body["tool_calls"][0]["name"] == "check_availability"
    assert body["tool_calls"][0]["result"]["quantity"] == 11
    assert body["session_id"] == "s1"


def test_a_blocked_turn_reports_its_violations_over_http(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug Phase 3 shipped lived here. The browser must see the block."""
    script(
        monkeypatch,
        calls("check_availability", {"item_query": "chocolate cake"}),
        says("About twenty."),
        says("Maybe thirty."),
    )

    body = client.post("/api/chat", json={"session_id": "s2", "message": "How many?"}).json()

    assert body["action"] == "safe_fallback"
    assert body["guardrail_tier"] == 1
    assert body["violations"]
    assert body["violations"][0]["rule"] == "quantity_grounding"


def test_a_conversation_persists_across_requests(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two posts with one session id are one call, not two."""
    script(monkeypatch, says("Hello, automated assistant here."), says("Still me."))

    first = client.post("/api/chat", json={"session_id": "s3", "message": "Hi"}).json()
    second = client.post("/api/chat", json={"session_id": "s3", "message": "Still there?"}).json()

    assert first["session_id"] == second["session_id"] == "s3"
    assert len(main._SESSIONS) == 1


def test_an_empty_message_is_a_client_error(client: TestClient) -> None:
    """Better a 400 than a turn spent on nothing."""
    assert client.post("/api/chat", json={"session_id": "s", "message": "   "}).status_code == 400


def test_a_model_outage_is_a_two_hundred_with_something_sayable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 on a phone line is silence. The caller gets a sentence instead."""

    def broken(messages: Any, *, system: str = "") -> ModelReply:
        raise llm.ModelUnavailableError("simulated outage")

    monkeypatch.setattr(llm, "call_model", broken)

    response = client.post("/api/chat", json={"session_id": "s4", "message": "Cakes?"})

    assert response.status_code == 200
    assert response.json()["action"] == "safe_fallback"


def test_ending_a_call_records_it_and_frees_the_session(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 8 joins agent behaviour to outcomes on call_id, so both ends matter."""
    script(monkeypatch, says("Hello."))
    client.post("/api/chat", json={"session_id": "s5", "message": "Hi"})

    assert client.post("/api/chat/s5/end").json() == {"ended": True}
    assert "s5" not in main._SESSIONS


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_the_dashboard_returns_one_consistent_picture(client: TestClient) -> None:
    """One endpoint, because two reads could disagree on screen."""
    body = client.get("/api/dashboard").json()

    assert len(body["stock"]) == 12
    assert body["pos_data_fresh"] is True
    assert body["location"]["name"]
    assert "events" in body


def test_the_dashboard_shows_the_sellable_arithmetic(client: TestClient) -> None:
    """The panel explains itself: on hand, held, buffer, and the result."""
    set_stock(CAKE, on_hand=10, safety_buffer=2)
    insert_hold(CAKE, 3, status="active")

    item = next(i for i in client.get("/api/dashboard").json()["stock"] if i["item_id"] == CAKE)

    assert (item["on_hand"], item["committed"], item["safety_buffer"], item["sellable"]) == (
        10,
        3,
        2,
        5,
    )


def test_stale_stock_reports_unknown_and_no_number(client: TestClient) -> None:
    """The staleness gate has to survive all the way to the browser."""
    inventory.force_stale(LOCATION_ID)

    body = client.get("/api/dashboard").json()

    assert body["pos_data_fresh"] is False
    assert all(item["status"] == "unknown" for item in body["stock"])
    assert all(item["sellable"] is None for item in body["stock"])


def test_a_hold_carries_a_countdown_and_a_local_pickup_time(client: TestClient) -> None:
    """Computed server-side, so the browser only has to count down."""
    insert_hold(CAKE, 1, status="active", expires_in_minutes=30)

    hold = client.get("/api/dashboard").json()["holds"][0]

    assert 0 < hold["expires_in_seconds"] <= 30 * 60
    assert hold["pickup_label"]
    assert "T" not in hold["pickup_label"]  # local wall time, not an ISO timestamp


def test_pending_holds_appear_alongside_active_ones(client: TestClient) -> None:
    """A staff view that hid them could not explain why availability looks low."""
    insert_hold(CAKE, 1, status="active")
    insert_hold(CAKE, 5, status="pending_approval")

    statuses = {h["status"] for h in client.get("/api/dashboard").json()["holds"]}

    assert statuses == {"active", "pending_approval"}


# ---------------------------------------------------------------------------
# Staff actions
# ---------------------------------------------------------------------------


def test_approving_a_hold_activates_it(client: TestClient) -> None:
    """The approval queue writes straight back to availability."""
    hold_id = insert_hold(CAKE, 5, status="pending_approval")

    body = client.post(f"/api/staff/holds/{hold_id}/approve").json()

    assert body["ok"] is True
    assert body["status"] == "active"


def test_declining_a_hold_releases_its_stock(client: TestClient) -> None:
    """Declined stock returns to the line immediately, not at expiry."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    hold_id = insert_hold(CAKE, 4, status="pending_approval")
    before = next(i for i in client.get("/api/dashboard").json()["stock"] if i["item_id"] == CAKE)[
        "sellable"
    ]

    client.post(f"/api/staff/holds/{hold_id}/decline")
    after = next(i for i in client.get("/api/dashboard").json()["stock"] if i["item_id"] == CAKE)[
        "sellable"
    ]

    assert before == 5
    assert after == 9


def test_collecting_a_hold_records_a_sale_and_queues_a_review_request(
    client: TestClient,
) -> None:
    """Collection is where the review request belongs, not hold time."""
    set_stock(CAKE, on_hand=10, safety_buffer=1)
    hold_id = insert_hold(CAKE, 2, status="active")

    assert client.post(f"/api/staff/holds/{hold_id}/collect").json()["ok"] is True

    kinds = {a["kind"] for a in client.get("/api/staff/alerts").json()["alerts"]}
    assert "review_request" in kinds
    row = db.query_one("SELECT on_hand FROM stock WHERE item_id = ?", (CAKE,))
    assert row is not None
    assert row["on_hand"] == 8


def test_acknowledging_an_alert_twice_is_not_an_error(client: TestClient) -> None:
    """The page polls and gets double-clicked."""
    insert_hold(CAKE, 1, status="active", expires_in_minutes=-5)
    client.post("/api/staff/holds/1/cancel")
    alerts = client.get("/api/staff/alerts").json()["alerts"]

    if alerts:
        alert_id = alerts[0]["id"]
        assert client.post(f"/api/staff/alerts/{alert_id}/ack").json()["acknowledged"] is True
        assert client.post(f"/api/staff/alerts/{alert_id}/ack").json()["acknowledged"] is False


def test_receiving_a_delivery_raises_the_sellable_count(client: TestClient) -> None:
    """The deliverable an interviewer checks: stock goes up on the customer page."""
    set_stock(CAKE, on_hand=4, safety_buffer=1)

    body = client.post(
        "/api/staff/delivery",
        json={"item_id": CAKE, "quantity": 6, "note": "afternoon bake"},
    ).json()

    assert body["on_hand"] == 10
    item = next(i for i in client.get("/api/dashboard").json()["stock"] if i["item_id"] == CAKE)
    assert item["sellable"] == 9


@pytest.mark.parametrize(
    "payload",
    [
        {"item_id": "", "quantity": 5},
        {"item_id": CAKE, "quantity": 0},
        {"item_id": CAKE, "quantity": -3},
        {"item_id": CAKE, "quantity": "lots"},
    ],
)
def test_a_nonsense_delivery_is_rejected(client: TestClient, payload: dict[str, Any]) -> None:
    """A delivery form wired to a domain function still validates its own input."""
    assert client.post("/api/staff/delivery", json=payload).status_code == 400


def test_a_delivery_for_an_unknown_item_is_a_client_error_not_a_crash(
    client: TestClient,
) -> None:
    """A domain refusal surfaces as a 400 rather than a 500 with a stack trace."""
    response = client.post("/api/staff/delivery", json={"item_id": "flying_saucer", "quantity": 2})

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Demo controls
# ---------------------------------------------------------------------------


def test_breaking_and_restoring_the_sync_both_work(client: TestClient) -> None:
    """Breaking things needs an undo, or recovering a demo means losing its state."""
    assert client.post("/api/simulate/stale").json()["stale"] is True
    assert client.get("/api/dashboard").json()["pos_data_fresh"] is False

    assert client.post("/api/simulate/restore").json()["stale"] is False
    assert client.get("/api/dashboard").json()["pos_data_fresh"] is True


def test_simulating_a_rush_moves_stock(client: TestClient) -> None:
    """Stock has to visibly move, or the glass box is a static picture."""
    before = {i["item_id"]: i["sellable"] for i in client.get("/api/dashboard").json()["stock"]}

    client.post("/api/simulate/sales", json={"minutes": 180})
    after = {i["item_id"]: i["sellable"] for i in client.get("/api/dashboard").json()["stock"]}

    assert any(after[key] < before[key] for key in before)


def test_resetting_clears_sessions_as_well_as_the_ledger(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surviving session would answer follow-ups about a hold that no longer exists."""
    script(monkeypatch, says("Hello."))
    client.post("/api/chat", json={"session_id": "s6", "message": "Hi"})
    assert main._SESSIONS

    client.post("/api/reset")

    assert not main._SESSIONS
    assert len(client.get("/api/dashboard").json()["stock"]) == 12


# ---------------------------------------------------------------------------
# The phone door, built in Phase 4 so Phase 6 is configuration
# ---------------------------------------------------------------------------


def test_the_voice_endpoint_dispatches_through_the_same_tools(client: TestClient) -> None:
    """One implementation, two front doors. The shapes must be identical."""
    body = client.post(
        "/api/voice/tool",
        json={
            "tool_name": "check_availability",
            "arguments": {"item_query": "chocolate cake"},
            "call_id": "phone_1",
        },
    ).json()

    assert body["name"] == "check_availability"
    assert body["result"]["quantity"] == 11
    assert body["ok"] is True


def test_a_phone_tool_call_appears_in_the_glass_box(client: TestClient) -> None:
    """The panel renders phone calls exactly as browser ones, because it reads events.

    This is what lets one demo show a phone conversation and a browser session side by
    side, which is the point of building the voice route before the voice platform.
    """
    client.post(
        "/api/voice/tool",
        json={
            "tool_name": "check_availability",
            "arguments": {"item_query": "chocolate cake"},
            "call_id": "phone_2",
        },
    )

    events = client.get("/api/dashboard").json()["events"]
    phone_calls = [e for e in events if e["kind"] == "tool_call" and e["channel"] == "phone"]

    assert len(phone_calls) == 1
    assert phone_calls[0]["call_id"] == "phone_2"


def test_a_phone_hold_reaches_the_same_ledger(client: TestClient) -> None:
    """The phone must not get its own stock, or the two channels oversell each other."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    client.post(
        "/api/voice/tool",
        json={
            "tool_name": "create_hold",
            "arguments": {"item_id": "chocolate cake", "quantity": 1, "pickup_time": "in an hour"},
            "call_id": "phone_3",
        },
    )

    holds_seen = client.get("/api/dashboard").json()["holds"]
    assert len(holds_seen) == 1
    assert holds_seen[0]["call_id"] == "phone_3"


def test_a_voice_call_without_a_tool_name_is_a_client_error(client: TestClient) -> None:
    """A malformed webhook should not read as a tool that failed."""
    assert client.post("/api/voice/tool", json={"arguments": {}}).status_code == 400


def test_a_voice_lifecycle_event_is_recorded_as_a_phone_call(client: TestClient) -> None:
    """So Phase 8 can count phone conversations without special-casing them."""
    client.post("/api/voice/events", json={"event": "call_started", "call_id": "phone_4"})

    events = client.get("/api/dashboard").json()["events"]
    started = [e for e in events if e["kind"] == "call_started"]

    assert started and started[0]["channel"] == "phone"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_reports_data_staleness_without_failing(client: TestClient) -> None:
    """Stale data is a data problem, not a liveness problem."""
    inventory.force_stale(LOCATION_ID)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["checks"]["pos_data_fresh"] is False


def test_health_says_whether_a_model_is_configured(client: TestClient) -> None:
    """Useful on a tunnel, where "why is it always falling back?" is the first question."""
    assert "model_configured" in client.get("/healthz").json()["checks"]


def test_an_unexpected_error_returns_a_shaped_response_and_leaks_nothing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception string can contain a path, a query, or a phone number.

    This response is rendered in a browser, so it says nothing but "internal error".
    """

    def explode() -> dict[str, Any]:
        raise RuntimeError("secret path C:/Users/someone/holdline and 3125550188")

    monkeypatch.setattr(main, "_pos_sync_age_seconds", explode)

    # raise_server_exceptions is a client option, not a per-request one: without it the
    # TestClient re-raises instead of letting the app's handler shape the response.
    with TestClient(main.app, raise_server_exceptions=False) as strict:
        response = strict.get("/healthz")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert "3125550188" not in response.text
