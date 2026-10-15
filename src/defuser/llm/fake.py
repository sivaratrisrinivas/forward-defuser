"""Test doubles for LLMProvider."""

import json
from collections.abc import Callable

from defuser.llm.provider import LLMError, LLMResponse, Usage


def text_response(obj, usage=(200, 100)) -> LLMResponse:
    body = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return LLMResponse(content=[{"type": "text", "text": body}], stop_reason="end_turn", usage=Usage(*usage))


def tool_response(*calls: tuple[str, dict], usage=(300, 80)) -> LLMResponse:
    blocks = [
        {"type": "tool_use", "id": f"tu_{i}_{name}", "name": name, "input": args}
        for i, (name, args) in enumerate(calls)
    ]
    return LLMResponse(content=blocks, stop_reason="tool_use", usage=Usage(*usage))


class ScriptedLLM:
    """Calls `script(purpose, kwargs, call_index)` for each request. Counts calls per purpose."""

    def __init__(self, script: Callable[[str, dict, int], LLMResponse | Exception]):
        self.script = script
        self.calls: list[tuple[str, dict]] = []

    async def complete(self, **kwargs) -> LLMResponse:
        purpose = kwargs.pop("purpose", "")
        n = sum(1 for p, _ in self.calls if p == purpose)
        self.calls.append((purpose, kwargs))
        out = self.script(purpose, kwargs, n)
        if isinstance(out, Exception):
            raise out
        if out is None:
            raise LLMError("script returned nothing")
        return out
