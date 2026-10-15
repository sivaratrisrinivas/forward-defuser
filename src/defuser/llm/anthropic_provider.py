"""Anthropic Messages API implementation of LLMProvider.

Notes from the current API docs (bundled claude-api skill, Oct 2026):
- Current models reject forced tool_choice (`any`/`tool`), so callers use `auto` and steer from the prompt.
- Thinking cannot be disabled on Opus 5.5; depth is set with `output_config.effort`.
- Structured output uses `output_config.format = {type: json_schema, schema}`.
- Assistant content blocks (including thinking blocks) are passed back unchanged on the next turn.
- Server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) is opt-in via
  LLM_REFUSAL_FALLBACK=1; a final `stop_reason: refusal` is surfaced as LLMError.
"""

import os

import anthropic

from defuser.config import get_settings
from defuser.llm.provider import LLMError, LLMResponse, Usage


class AnthropicProvider:
    _shared: "AnthropicProvider | None" = None

    def __init__(self, client: anthropic.AsyncAnthropic | None = None):
        s = get_settings()
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else None
        self.client = client or anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=60)
        self.fallback = os.environ.get("LLM_REFUSAL_FALLBACK", "0") == "1"

    @classmethod
    def shared(cls) -> "AnthropicProvider":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    async def complete(
        self,
        *,
        model,
        system,
        messages,
        tools=None,
        json_schema=None,
        max_tokens=4000,
        effort=None,
        purpose="",
    ) -> LLMResponse:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        output_config: dict = {}
        if effort:
            output_config["effort"] = effort
        if json_schema:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        if tools:
            kwargs["tools"] = tools
        try:
            if self.fallback and not model.startswith("claude-haiku"):
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            raise LLMError(f"anthropic {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic connection error: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("model refused")
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        content = [b.model_dump(mode="json", exclude_none=True) for b in resp.content]
        return LLMResponse(content=content, stop_reason=resp.stop_reason or "", usage=usage, model=resp.model)
