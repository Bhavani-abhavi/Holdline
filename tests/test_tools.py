"""Tool dispatch: matching, parsing, policy in tools, event logging."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from holdline import clock, inventory
from holdline.tools import ToolContext, dispatch, match_item, parse_pickup

CTX = ToolContext(location_id="marchettis_lincoln_park", call_id="call-1", channel="browser")


@pytest.mark.parametrize(
    "query,item_id",
    [
        ("chocolate cake", "cake_choc_9"),
        ("do you have any chocolate cakes left", "cake_choc_9"),
        ("sourdough", "bread_sourdough"),
        ("a couple of almond croissants", "croissant_almond"),
        ("peanut butter cookies", "cookie_pb"),
        ("cheesecake", "cheesecake_ny"),
    ],
)
def test_match_item_clear_cases(db, query, item_id):
    with db.connection() as conn:
        status, cands = match_item(conn, query)
    assert status == "match" and cands[0]["item_id"] == item_id


def test_match_item_ambiguous_and_none(db):
    with db.connection() as conn:
        status, cands = match_item(conn, "the chocolate one")
        assert status == "ambiguous" and len(cands) >= 2
        assert match_item(conn, "pizza")[0] == "no_match"


def test_parse_pickup_forms():
    now = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)  # 10:00 Chicago
    tz = ZoneInfo("America/Chicago")
    assert parse_pickup("5pm", now, tz).astimezone(tz).hour == 17
    assert parse_pickup("five", now, tz) is None  # words are the model's job; it sends '5pm'
    assert parse_pickup("17:30", now, tz).astimezone(tz).minute == 30
    assert parse_pickup("in 45 minutes", now, tz) == now.replace(hour=15, minute=45)
    assert parse_pickup("in 2 hours", now, tz).hour == 17
    assert parse_pickup("noon", now, tz).astimezone(tz).hour == 12
    assert parse_pickup("5", now, tz).astimezone(tz).hour == 17  # bakery callers mean 5pm
    assert parse_pickup("2026-10-18T16:00:00-05:00", now, tz).astimezone(tz).hour == 16
    assert parse_pickup("whenever", now, tz) is None


def test_check_availability_returns_known_and_logs_event(db):
    result = dispatch(db, CTX, "check_availability", {"item_query": "chocolate cake"})
    assert result.ok and result.output["status"] == "known" and result.output["quantity"] == 10
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE kind = 'tool_call' ORDER BY id DESC"
        ).fetchone()
    assert row["call_id"] == "call-1" and row["channel"] == "browser"


def test_check_availability_stale_has_no_quantity(db):
    inventory.force_stale(db, CTX.location_id)
    result = dispatch(db, CTX, "check_availability", {"item_query": "chocolate cake"})
    assert result.output["status"] == "unknown" and "quantity" not in result.output


def test_list_menu_never_has_quantities(db):
    result = dispatch(db, CTX, "list_menu", {})
    assert len(result.output["items"]) == 12
    assert all("quantity" not in item and "on_hand" not in item for item in result.output["items"])


def test_create_hold_through_tool_applies_policy(db):
    small = dispatch(
        db,
        CTX,
        "create_hold",
        {"item_id": "cake_choc_9", "quantity": 1, "pickup_time": "in 1 hour"},
    )
    assert small.output["ok"] and small.output["status"] == "active"
    large = dispatch(
        db,
        CTX,
        "create_hold",
        {"item_id": "cake_choc_9", "quantity": 4, "pickup_time": "in 1 hour"},
    )
    assert large.output["status"] == "pending_approval"
    allergy = dispatch(
        db,
        CTX,
        "create_hold",
        {
            "item_id": "cake_carrot",
            "quantity": 1,
            "pickup_time": "in 1 hour",
            "dietary_note": "my son is allergic to walnuts",
        },
    )
    assert allergy.output["status"] == "pending_approval"
    assert "allergy_declared" in allergy.output["approval_reasons"]


def test_create_hold_unclear_time_asks_instead_of_guessing(db):
    result = dispatch(
        db, CTX, "create_hold", {"item_id": "cake_choc_9", "quantity": 1, "pickup_time": "later"}
    )
    assert result.output["ok"] is False and result.output["reason"] == "pickup_time_unclear"


def test_phone_is_fingerprinted_in_event_log(db):
    dispatch(
        db,
        CTX,
        "create_hold",
        {
            "item_id": "cake_choc_9",
            "quantity": 1,
            "pickup_time": "in 1 hour",
            "customer_phone": "312-555-0199",
        },
    )
    with db.connection() as conn:
        rows = conn.execute("SELECT detail FROM events").fetchall()
    assert not any("555-0199" in r["detail"] for r in rows)


def test_unknown_tool_and_bad_args_do_not_raise(db):
    assert dispatch(db, CTX, "fly_to_moon", {}).ok is False
    assert dispatch(db, CTX, "create_hold", {"item_id": "cake_choc_9"}).ok is False


def test_escalate_creates_alert(db):
    result = dispatch(
        db, CTX, "escalate_to_staff", {"reason": "caller asked for a person", "urgency": "action"}
    )
    assert result.output["ok"]
    with db.connection() as conn:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM staff_alerts")]
    assert "escalation" in kinds


def test_store_info(db):
    out = dispatch(db, CTX, "get_store_info", {}).output
    assert "Halsted" in out["address"] and "7pm" in out["hours"]
    _ = clock.now()
