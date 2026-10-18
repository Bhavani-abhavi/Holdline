"""Allergy policy: always escalates, always pending_approval, never a safety claim."""

from __future__ import annotations

from datetime import timedelta

import pytest

from holdline import clock
from holdline.dietary import assess_request, check_dietary_info
from holdline.holds import create_hold
from holdline.notifications import open_alerts


@pytest.mark.parametrize(
    "text",
    [
        "my daughter is allergic to peanuts",
        "I have a nut allergy",
        "celiac",
        "coeliac disease",
        "lactose intolerant",
        "my son can't have gluten, will this work?",
        "she carries an EpiPen",
        "he gets a reaction to eggs",
        "is this safe for a peanut allergy",
        "I need it to be gluten free",
    ],
)
def test_allergy_phrases_require_human(text):
    assessment = assess_request(text)
    assert assessment.severity == "allergy"
    assert assessment.requires_human is True


@pytest.mark.parametrize(
    "text", ["I'm vegan", "vegetarian please", "trying to avoid sugar", "no nuts"]
)
def test_preferences_do_not_require_human(text):
    assessment = assess_request(text)
    assert assessment.severity == "preference"
    assert assessment.requires_human is False


def test_empty_is_none():
    assert assess_request("").severity == "none"
    assert assess_request(None).severity == "none"


def test_check_dietary_info_relays_declarations_only(db):
    with db.connection() as conn:
        info = check_dietary_info(conn, "brownie_flourless")
    data = info.to_dict()
    assert "gluten-free" in data["dietary_flags"]
    assert "wheat" in data["may_contain"]  # the flag is not a safety claim
    assert "safe" not in {k.lower() for k in data}  # no computed safety field exists
    assert "Never state" in data["policy"]


def test_allergy_hold_is_pending_with_urgent_callback(db, location_id):
    result = create_hold(
        db,
        location_id=location_id,
        item_id="cake_choc_9",
        quantity=1,
        pickup_at=clock.now() + timedelta(hours=1),
        call_id="c",
        dietary_note="daughter allergic to peanuts",
    )
    assert result.ok
    assert result.status == "pending_approval"
    assert "allergy_declared" in result.approval_reasons
    with db.connection() as conn:
        alerts = open_alerts(conn, location_id)
    assert alerts[0]["kind"] == "allergy_callback"
    assert alerts[0]["urgency"] == "urgent"
    assert alerts[0]["payload"]["dietary_note"] == "daughter allergic to peanuts"


def test_preference_note_still_goes_to_staff_as_pending(db, location_id):
    result = create_hold(
        db,
        location_id=location_id,
        item_id="bread_sourdough",
        quantity=1,
        pickup_at=clock.now() + timedelta(hours=1),
        call_id="c",
        dietary_note="vegan",
    )
    assert result.status == "pending_approval"
    assert "dietary_note_present" in result.approval_reasons
