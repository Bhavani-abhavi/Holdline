"""The phone layer: guardrail parity, webhook verification, and what the tunnel exposes.

All offline. `llm.call_model` is replaced throughout, because the thing being tested is
that a phone turn goes through the *same* agent and the *same* validator as a browser
turn -- which is a property of the wiring, not of the model.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from holdline import config, db, guardrails, llm, main, tools, voice
from holdline.llm import ModelReply, ModelToolCall

from helpers import CAKE, alerts_of_kind, count_live_holds, set_stock


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


def chat_request(said: str, *, call_id: str = "phone_1") -> dict[str, Any]:
    """The body shape a voice platform posts in custom-LLM mode."""
    return {
        "model": "custom",
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": said},
        ],
        "call": {"id": call_id},
        "stream": False,
    }


@pytest.fixture
def phone(seeded_db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client for the voice-only application, with no live model."""
    monkeypatch.setattr(llm, "call_model", Script())
    monkeypatch.setattr(config, "VAPI_WEBHOOK_SECRET", "")
    main._SESSIONS.clear()
    with TestClient(main.voice_app) as client:
        yield client
    main._SESSIONS.clear()


def script(monkeypatch: pytest.MonkeyPatch, *replies: ModelReply) -> None:
    """Install a scripted provider for one test."""
    monkeypatch.setattr(llm, "call_model", Script(*replies))


# ---------------------------------------------------------------------------
# What a public tunnel can reach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/staff", "/analytics", "/api/dashboard", "/api/staff/alerts", "/static/staff.html"],
)
def test_the_voice_app_does_not_serve_the_staff_surface(phone: TestClient, path: str) -> None:
    """The constraint, enforced by absence rather than by a check.

    `/staff` has no authentication and shows customer phone numbers in the clear, which
    is correct behind a counter and unacceptable on a public URL. Blocking it with
    middleware would leave it one bad conditional away from being world-readable; not
    registering it means the process facing the internet has never heard of it.
    """
    assert phone.get(path).status_code == 404


def test_the_voice_app_does_not_serve_the_browser_chat(phone: TestClient) -> None:
    """The browser endpoint would let anyone with the URL spend the model budget."""
    assert phone.post("/api/chat", json={"message": "hi"}).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_voice_app_publishes_no_api_documentation(phone: TestClient, path: str) -> None:
    """FastAPI serves these by default, advertising every endpoint to whoever finds it."""
    assert phone.get(path).status_code == 404


def test_the_voice_app_serves_exactly_the_phone_layer() -> None:
    """Stated as a whole-surface assertion, so a new route cannot be added unnoticed."""
    assert {route.path for route in main.voice_app.routes} == {
        "/",
        "/healthz",
        "/v1/chat/completions",
        "/api/voice/tool",
        "/api/voice/events",
    }


def test_the_full_app_still_serves_everything(seeded_db: Path) -> None:
    """Splitting the public surface must not have cost the demo its pages."""
    paths = {route.path for route in main.app.routes}

    assert {"/", "/staff", "/analytics", "/api/chat", "/api/dashboard"} <= paths


def test_the_voice_root_explains_itself(phone: TestClient) -> None:
    """A stray visitor should get an explanation, not a mystery 404."""
    body = phone.get("/").json()

    assert body["service"] == "holdline-voice"
    assert "staff" in body["note"].lower()


# ---------------------------------------------------------------------------
# Guardrail parity
# ---------------------------------------------------------------------------


def test_a_phone_turn_runs_the_real_tools_and_the_real_ledger(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One implementation, two front doors. The phone must not get its own stock."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    script(
        monkeypatch,
        calls(
            "create_hold",
            {"item_id": "chocolate cake", "quantity": 1, "pickup_time": "in an hour"},
        ),
        says("You're all set, I've put one aside."),
    )

    response = phone.post("/v1/chat/completions", json=chat_request("Hold me a chocolate cake"))

    assert response.status_code == 200
    assert count_live_holds(CAKE) == 1


def test_the_guardrail_blocks_an_unsafe_phone_reply(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason custom-LLM mode exists at all.

    If the platform generated the reply, this sentence would reach a caller and a
    platform-side response hook would fire after the fact. Generating through our own
    endpoint means the validator is a veto rather than a notification.
    """
    script(
        monkeypatch,
        calls("check_dietary_info", {"item_id": "chocolate cake"}),
        says("That one is completely peanut-free, she'll be fine."),
    )
    body = chat_request("My daughter is allergic to peanuts, is it safe?")

    spoken = phone.post("/v1/chat/completions", json=body).json()["choices"][0]["message"][
        "content"
    ]

    assert spoken == guardrails.SAFETY_ESCALATION
    assert "peanut-free" not in spoken


def test_an_ungrounded_phone_reply_is_corrected_before_it_is_spoken(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1 retries on the phone exactly as it does in the browser."""
    script(
        monkeypatch,
        calls("check_availability", {"item_query": "chocolate cake"}),
        says("About twenty."),
        says("We have eleven right now."),
    )

    spoken = phone.post("/v1/chat/completions", json=chat_request("How many?")).json()["choices"][
        0
    ]["message"]["content"]

    assert spoken == "We have eleven right now."


def test_a_phone_turn_is_recorded_against_its_call_id(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The glass box renders phone and browser identically, which needs the id."""
    script(
        monkeypatch, calls("check_availability", {"item_query": "chocolate cake"}), says("Eleven.")
    )

    phone.post("/v1/chat/completions", json=chat_request("How many?", call_id="phone_xyz"))

    # call_id is its own column, not part of the JSON detail blob.
    rows = db.query("SELECT kind, channel, call_id FROM events WHERE call_id = ?", ("phone_xyz",))
    kinds = {row["kind"] for row in rows}

    assert {"call_started", "tool_call"} <= kinds
    assert all(row["channel"] == "phone" for row in rows if row["channel"])


def test_the_response_is_shaped_like_a_chat_completion(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform parses this as an OpenAI response, so the shape is a contract."""
    script(monkeypatch, says("Hello, automated assistant here."))

    body = phone.post("/v1/chat/completions", json=chat_request("Hi")).json()

    assert body["object"] == "chat.completion"
    assert body["choices"][0]["index"] == 0
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}


def test_streaming_is_answered_in_one_piece(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requested streaming is deliberately ignored.

    The validator judges a complete reply, and a streamed one has been partly spoken
    before it could be judged. One second of latency buys the whole guarantee.
    """
    script(monkeypatch, says("Hello there."))
    body = chat_request("Hi")
    body["stream"] = True

    response = phone.post("/v1/chat/completions", json=body)

    assert response.status_code == 200
    assert response.json()["object"] == "chat.completion"
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_a_request_with_no_user_message_is_rejected(phone: TestClient) -> None:
    """Nothing to answer is a client error, not an empty turn charged to the budget."""
    body = {"messages": [{"role": "system", "content": "..."}], "call": {"id": "c"}}

    assert phone.post("/v1/chat/completions", json=body).status_code == 400


def test_a_model_outage_still_returns_something_sayable(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 to the platform becomes dead air on a live call."""

    def broken(messages: Any, *, system: str = "") -> ModelReply:
        raise llm.ModelUnavailableError("simulated outage")

    monkeypatch.setattr(llm, "call_model", broken)

    response = phone.post("/v1/chat/completions", json=chat_request("Cakes?"))

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == guardrails.SAFE_FALLBACK


# ---------------------------------------------------------------------------
# Webhook verification
# ---------------------------------------------------------------------------


def test_an_unverified_request_is_refused_when_a_secret_is_set(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool route writes to the ledger, so reaching the tunnel is not authorisation."""
    monkeypatch.setattr(config, "VAPI_WEBHOOK_SECRET", "s3cret")

    response = phone.post(
        "/api/voice/tool",
        json={"tool_name": "check_availability", "arguments": {"item_query": "cake"}},
    )

    assert response.status_code == 401


def test_the_right_secret_is_accepted(phone: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side of the same check."""
    monkeypatch.setattr(config, "VAPI_WEBHOOK_SECRET", "s3cret")

    response = phone.post(
        "/api/voice/tool",
        json={"tool_name": "check_availability", "arguments": {"item_query": "chocolate cake"}},
        headers={"x-vapi-secret": "s3cret"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["quantity"] == 11


def test_the_chat_endpoint_is_verified_too(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It spends the model budget, so an open one is a bill anyone can run up."""
    monkeypatch.setattr(config, "VAPI_WEBHOOK_SECRET", "s3cret")

    assert phone.post("/v1/chat/completions", json=chat_request("Hi")).status_code == 401


def test_verification_passes_loudly_when_no_secret_is_configured() -> None:
    """Right default for a local tunnel, and it says so in the log every time."""
    assert voice.verify_webhook(None, None, b"") is True
    assert voice.verify_webhook("", "anything", b"") is True


def test_a_body_signature_is_accepted_as_well_as_an_echoed_secret() -> None:
    """Platforms differ, and a configuration change should not reject every call."""
    secret = "s3cret"  # noqa: S105 -- a fixture value, not a credential
    body = b'{"hello":"world"}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert voice.verify_webhook(secret, signature, body) is True
    assert voice.verify_webhook(secret, "wrong", body) is False


# ---------------------------------------------------------------------------
# Configuration generated from one source
# ---------------------------------------------------------------------------


def test_the_assistant_is_configured_for_custom_llm_mode() -> None:
    """The single field that keeps the guardrail on the phone path."""
    settings = voice.vapi_assistant_config(public_base_url="https://example.ngrok.app")

    assert settings["model"]["provider"] == "custom-llm"
    assert settings["model"]["url"] == "https://example.ngrok.app/v1"


def test_the_platform_is_given_no_tools_of_its_own() -> None:
    """Our agent runs the tool loop. A platform holding tools would run its own."""
    settings = voice.vapi_assistant_config(public_base_url="https://example.ngrok.app")

    assert settings["model"]["tools"] == []


def test_the_tool_schemas_still_come_from_the_single_source() -> None:
    """Unused in this mode, generated anyway, so switching modes cannot drift them."""
    names = [t["function"]["name"] for t in voice.openai_tool_definitions()]

    assert names == [spec.name for spec in tools.TOOL_SPECS]


def test_the_assistant_discloses_that_it_is_automated_on_the_first_turn() -> None:
    """Said by the platform before our model speaks at all, so it cannot be skipped."""
    settings = voice.vapi_assistant_config(public_base_url="https://x.test")

    assert "automated" in settings["firstMessage"].lower()


def test_the_caller_hears_something_while_a_tool_runs() -> None:
    """Silence on a phone line reads as a dropped call."""
    settings = voice.vapi_assistant_config(public_base_url="https://x.test")

    assert settings["fillerInjectionEnabled"] is True
    assert len(settings["customFillerPhrases"]) > 1


def test_a_trailing_slash_in_the_base_url_does_not_double_up() -> None:
    """Copied from a browser bar, these usually have one."""
    settings = voice.vapi_assistant_config(public_base_url="https://x.test/")

    assert settings["model"]["url"] == "https://x.test/v1"
    assert "//api" not in settings["serverUrl"].removeprefix("https://")


# ---------------------------------------------------------------------------
# Reading what the platform sends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"call_id": "abc"},
        {"call": {"id": "abc"}},
        {"metadata": {"call_id": "abc"}},
        {"message": {"call": {"id": "abc"}}},
    ],
)
def test_the_call_id_is_found_wherever_the_platform_put_it(payload: dict[str, Any]) -> None:
    """Vapi has moved this field between releases and nests it differently per request.

    Losing it means a conversation cannot be traced end to end, and Phase 8 joins agent
    behaviour to business outcomes on exactly this value.
    """
    assert voice.call_id_from_request(payload) == "abc"


def test_a_missing_call_id_is_reported_rather_than_invented() -> None:
    """A fabricated id would silently split one conversation across two traces."""
    assert voice.call_id_from_request({}) is None


def test_only_the_latest_caller_message_is_taken() -> None:
    """The platform resends the whole history; the agent already has its own."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]

    assert voice.latest_user_message(messages) == "second"


def test_content_sent_as_parts_is_read_too() -> None:
    """Some clients send content as a list rather than a string."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello there"}]}]

    assert voice.latest_user_message(messages) == "hello there"


def test_an_empty_history_yields_nothing_rather_than_raising() -> None:
    """A malformed request is a 400, decided by the route, not an exception here."""
    assert voice.latest_user_message([]) == ""


def test_token_counts_are_reported_as_zero_rather_than_invented() -> None:
    """Our spend belongs to our own model call, not to the platform's accounting."""
    body = voice.chat_completion_response("hello", model="claude-opus-5")

    assert body["usage"]["total_tokens"] == 0


# ---------------------------------------------------------------------------
# Both channels reach the same place
# ---------------------------------------------------------------------------


def test_a_phone_allergy_raises_the_same_urgent_alert_as_a_browser_one(
    phone: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Staff must not have to watch two queues, and safety must not depend on channel."""
    set_stock(CAKE, on_hand=20, safety_buffer=1)
    script(
        monkeypatch,
        calls(
            "create_hold",
            {
                "item_id": "chocolate cake",
                "quantity": 1,
                "pickup_time": "in an hour",
                "customer_name": "Priya",
                "dietary_note": "my son is allergic to peanuts",
            },
        ),
        says("I'm putting a soft hold on it and having someone call you back."),
    )

    phone.post("/v1/chat/completions", json=chat_request("Hold a cake, son has a nut allergy"))

    assert len(alerts_of_kind("allergy_callback")) == 1
