"""Notification fan-out: every channel tried, one failure never silences the rest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from holdline import config, db, holds, notifications

from helpers import CAKE, LOCATION_ID, set_stock


class RecordingNotifier:
    """Captures what it was asked to send, for assertions."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.sent: list[tuple[str, str, str]] = []

    def send(self, kind: str, urgency: str, summary: str, payload: dict[str, Any]) -> None:
        """Record the call and succeed."""
        self.sent.append((kind, urgency, summary))


class BrokenNotifier:
    """Fails on every call, the way a third-party client does during an outage."""

    def send(self, kind: str, urgency: str, summary: str, payload: dict[str, Any]) -> None:
        """Always raises."""
        raise RuntimeError("upstream notification service is unreachable")


def test_a_notification_reaches_every_configured_channel(seeded_db: Path) -> None:
    """Console and in-app are both on by default, and both must fire."""
    delivered = notifications.notify("new_hold", "info", "2 x Chocolate Cake held")

    assert delivered == len(config.ENABLED_NOTIFIERS)
    rows = db.query("SELECT kind, urgency, summary FROM staff_alerts")
    assert len(rows) == 1
    assert rows[0]["kind"] == "new_hold"


def test_one_broken_channel_does_not_stop_the_others(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation is the whole reason the fan-out catches exceptions.

    A bakery cannot stop taking orders because a notification channel broke, so the
    failure is logged and the remaining channels still deliver.
    """
    working = RecordingNotifier()
    monkeypatch.setattr(
        notifications, "get_notifiers", lambda: [BrokenNotifier(), working, BrokenNotifier()]
    )

    delivered = notifications.notify("approval_needed", "action", "Approve 5 x cake?")

    assert delivered == 1
    assert len(working.sent) == 1


def test_a_hold_still_completes_when_every_channel_fails(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's promise does not depend on staff being successfully notified.

    Losing the notification is bad and is logged at ERROR. Losing the hold would be
    worse: the customer was already told the cake is theirs.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    monkeypatch.setattr(notifications, "get_notifiers", lambda: [BrokenNotifier()])

    result = holds.create_hold(LOCATION_ID, CAKE, 1, customer_name="Dana")

    assert result.ok is True
    assert result.hold_id is not None


def test_total_notification_failure_is_never_reported_as_success(
    seeded_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence must be distinguishable from delivery."""
    monkeypatch.setattr(notifications, "get_notifiers", lambda: [BrokenNotifier()])

    assert notifications.notify("allergy_callback", "urgent", "call back") == 0


def test_the_sms_channel_refuses_rather_than_pretending_to_send() -> None:
    """A stub that returned successfully would be the worst outcome here.

    An undelivered allergy callback would look identical to a delivered one. Raising
    keeps the two distinguishable, and the fan-out already isolates the failure so it
    cannot take a hold down.
    """
    with pytest.raises(NotImplementedError, match="documented stub"):
        notifications.SmsNotifier().send("allergy_callback", "urgent", "call back", {})


def test_an_unknown_channel_name_degrades_notifications_rather_than_the_bakery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in a deployment's notifier list must not stop the phone being answered."""
    monkeypatch.setattr(config, "ENABLED_NOTIFIERS", ("console", "carrier_pigeon"))

    active = notifications.get_notifiers()

    assert len(active) == 1
    assert isinstance(active[0], notifications.ConsoleNotifier)


def test_open_alerts_put_the_urgent_ones_first(seeded_db: Path) -> None:
    """Staff read from the top, so ordering is a safety feature rather than a nicety."""
    notifications.notify("new_hold", "info", "routine hold")
    notifications.notify("approval_needed", "action", "needs a decision")
    notifications.notify("allergy_callback", "urgent", "ALLERGY -- call back")

    queue = notifications.open_alerts()

    assert [alert["urgency"] for alert in queue] == ["urgent", "action", "info"]


def test_acknowledging_an_alert_removes_it_from_the_queue(seeded_db: Path) -> None:
    """The queue is what staff work through, so handled items have to leave it."""
    notifications.notify("new_hold", "info", "routine hold")
    alert_id = int(notifications.open_alerts()[0]["id"])

    assert notifications.acknowledge(alert_id) is True
    assert notifications.open_alerts() == []


def test_acknowledging_twice_is_not_an_error(seeded_db: Path) -> None:
    """The staff page polls and gets double-clicked; neither may produce a failure."""
    notifications.notify("new_hold", "info", "routine hold")
    alert_id = int(notifications.open_alerts()[0]["id"])

    first = notifications.acknowledge(alert_id)
    second = notifications.acknowledge(alert_id)

    assert first is True
    assert second is False


def test_the_alert_payload_keeps_the_number_staff_need_to_dial(seeded_db: Path) -> None:
    """`staff_alerts` is an operational queue, not an analytics export.

    Staff have to be able to call an allergy customer back, so the real number lives
    here. The logging processor redacts it on every path to a log file, and the event
    log -- which *is* exported -- stores only a redacted form.
    """
    set_stock(CAKE, on_hand=20, safety_buffer=1)

    holds.create_hold(
        LOCATION_ID,
        CAKE,
        1,
        customer_name="Priya",
        customer_phone="+13125550188",
        dietary_note="peanut allergy",
    )

    callback = next(a for a in notifications.open_alerts() if a["kind"] == "allergy_callback")
    assert callback["payload"]["customer_phone"] == "+13125550188"
