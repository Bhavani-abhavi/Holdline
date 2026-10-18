"""The one place the model is called.

Everything above this module speaks in plain dicts: a list of content blocks
(`text` and `tool_use`), a stop reason and a usage dict. That boundary is what
lets tests use `FakeLLM`, the eval harness replay recordings with zero API
calls, and a different provider be swapped in without touching the agent.

Cost controls live here too: a prompt-cache breakpoint at the end of the static
prefix (tools + system prompt), a hard `max_tokens`, an explicit timeout, and a
usage record on every response so the eval harness can report measured cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from holdline import config
from holdline.exceptions import LLMUnavailableError
from holdline.logging_setup import get_logger

log = get_logger(__name__)

# Published list prices per million tokens, used only to REPORT measured spend in
# evals/RESULTS.md. (input, output, cache_read, cache_write). Source: Anthropic
# pricing page as cached in the claude-api reference on 2026-10-06.
PRICES_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}


@dataclass
class Usage:
    """Token counts for one call. Cached reads are the cheap ones."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }

    def cost_usd(self, model: str) -> float | None:
        """Dollar cost at list price, or None for a model with no price on file."""
        prices = PRICES_PER_MTOK.get(model)
        if prices is None:
            return None
        inp, out, read, write = prices
        return (
            self.input_tokens * inp
            + self.output_tokens * out
            + self.cache_read_input_tokens * read
            + self.cache_creation_input_tokens * write
        ) / 1_000_000


@dataclass
class LLMResponse:
    """Provider-neutral response. `content` holds plain dict blocks."""

    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "stop_reason": self.stop_reason,
            "usage": self.usage.to_dict(),
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResponse:
        return cls(
            content=list(data["content"]),
            stop_reason=str(data["stop_reason"]),
            usage=Usage(**data.get("usage", {})),
            model=str(data.get("model", "")),
        )


class LLMClient(Protocol):
    """Anything that can answer one turn."""

    def complete(
        self,
        *,
        system_static: str,
        system_dynamic: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = config.LLM_MAX_TOKENS,
    ) -> LLMResponse: ...


def _thinking_params(model: str) -> dict[str, Any]:
    """Per-model request shape for a short, tool-heavy, low-latency reply.

    Phone replies are capped at a few hundred tokens, and thinking tokens count
    toward that cap, so thinking is turned off where the model allows it and set
    to the lowest effort elsewhere. The table follows the claude-api reference.
    """
    if model.startswith("claude-sonnet-5-5"):
        return {"thinking": {"type": "between_tools"}}
    if model.startswith("claude-haiku-5-5"):
        return {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
    return {"output_config": {"effort": "low"}}


class AnthropicLLM:
    """Production client over the official SDK."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        import anthropic  # imported here so tests never need the SDK on the hot path

        self.model = model
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=config.LLM_TIMEOUT_SECONDS, max_retries=1
        )
        self._anthropic = anthropic

    def complete(
        self,
        *,
        system_static: str,
        system_dynamic: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = config.LLM_MAX_TOKENS,
    ) -> LLMResponse:
        # Render order is tools -> system -> messages. One breakpoint at the end of the
        # static system block caches the tool schemas and the persona together; the
        # dynamic block (current time) sits after it so it never invalidates the cache.
        system = [
            {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system_dynamic},
        ]
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                **_thinking_params(self.model),
            )
        except self._anthropic.RateLimitError as exc:
            raise LLMUnavailableError(f"rate limited: {exc.message}") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"api error {exc.status_code}: {exc.message}") from exc
        except (self._anthropic.APIConnectionError, self._anthropic.APITimeoutError) as exc:
            raise LLMUnavailableError(f"connection error: {exc}") from exc

        content: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input),
                    }
                )
            # thinking blocks are intentionally dropped: they are empty under the
            # settings above and the agent never replays them.
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        log.info(
            "llm_call",
            model=self.model,
            stop_reason=response.stop_reason,
            cache_hit=usage.cache_read_input_tokens > 0,
            **usage.to_dict(),
        )
        return LLMResponse(
            content=content, stop_reason=str(response.stop_reason), usage=usage, model=self.model
        )


class FakeLLM:
    """Scripted client for tests. Hands out queued responses in order and records calls."""

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self.responses: list[LLMResponse] = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.model = "fake"

    def queue(self, *responses: LLMResponse) -> None:
        self.responses.extend(responses)

    def complete(
        self,
        *,
        system_static: str,
        system_dynamic: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = config.LLM_MAX_TOKENS,
    ) -> LLMResponse:
        self.calls.append({"system": system_static, "messages": list(messages), "tools": tools})
        if not self.responses:
            raise LLMUnavailableError("FakeLLM has no scripted response left")
        return self.responses.pop(0)


# --- Helpers for building scripted responses ------------------------------------------

_counter = 0


def text_response(text: str) -> LLMResponse:
    """A plain final reply."""
    return LLMResponse(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def tool_response(*calls: tuple[str, dict[str, Any]], text: str = "") -> LLMResponse:
    """A response requesting one or more tool calls (optionally with lead-in text)."""
    global _counter
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for name, args in calls:
        _counter += 1
        content.append(
            {"type": "tool_use", "id": f"toolu_fake_{_counter}", "name": name, "input": args}
        )
    return LLMResponse(content=content, stop_reason="tool_use")
