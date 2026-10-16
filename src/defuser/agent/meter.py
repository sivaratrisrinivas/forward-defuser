"""Per-run cost and step accounting."""

from dataclasses import dataclass, field

from defuser.llm.provider import LLMResponse, Usage, cost_usd


@dataclass
class Meter:
    max_cost: float
    max_steps: int
    cost: float = 0.0
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls: list[dict] = field(default_factory=list)

    def add(self, purpose: str, resp: LLMResponse | dict, model: str) -> None:
        if isinstance(resp, dict):
            usage = Usage(**resp["usage"])
        else:
            usage = resp.usage
        c = cost_usd(model, usage)
        self.cost += c
        self.input_tokens += usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        self.calls.append({"purpose": purpose, "model": model, "cost": round(c, 6)})

    @property
    def over(self) -> bool:
        return self.cost >= self.max_cost or self.steps >= self.max_steps
