"""One thin LLM interface. Everything model-specific lives in the implementations."""

from dataclasses import dataclass, field
from typing import Protocol

from defuser.config import get_settings


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    content: list[dict]  # API content blocks as plain dicts (text, tool_use, thinking ...)
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLMError(Exception):
    """Provider failed, refused, or returned unusable output. Callers fall back to UNVERIFIED."""


class LLMProvider(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        max_tokens: int = 4000,
        effort: str | None = None,
        purpose: str = "",
    ) -> LLMResponse: ...


def cost_usd(model: str, usage: Usage) -> float:
    prices = get_settings().prices()
    pin, pout = prices.get(model, max(prices.values(), default=(5.0, 25.0)))
    # Cache reads bill at ~0.1x input and cache writes at ~1.25x input (Anthropic prompt caching pricing).
    billed_in = usage.input_tokens + 0.1 * usage.cache_read_tokens + 1.25 * usage.cache_write_tokens
    return (billed_in * pin + usage.output_tokens * pout) / 1_000_000


_override: LLMProvider | None = None


def set_llm(provider: LLMProvider | None) -> None:
    global _override
    _override = provider


def get_llm() -> LLMProvider:
    if _override is not None:
        return _override
    s = get_settings()
    if s.llm_provider == "anthropic":
        from defuser.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider.shared()
    raise LLMError(f"LLM provider {s.llm_provider!r} has no implementation configured")
