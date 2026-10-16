"""The investigation agent: a journaled tool loop bounded by step and cost budgets."""

import json
import logging
from dataclasses import dataclass, field

from defuser.agent.journal import Journal, step_key
from defuser.agent.meter import Meter
from defuser.agent.prompts import INVESTIGATE_SYSTEM
from defuser.agent.tools import Toolbox, ToolOutcome, wrap_untrusted
from defuser.config import get_settings
from defuser.llm.provider import LLMError, LLMProvider, LLMResponse, Usage
from defuser.models import CanonicalMessage, TriageResult

log = logging.getLogger(__name__)


@dataclass
class Investigation:
    verdict: str = "UNVERIFIED"
    confidence: float = 0.0
    rationale: str = ""
    finished: bool = False
    stop: str = ""  # finished | budget | steps | llm_error | no_finish
    followup: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


def render_case(msg: CanonicalMessage, triage: TriageResult) -> str:
    claims = "\n".join(f"{i + 1}. {c.text}  [English: {c.canonical_en}]" for i, c in enumerate(triage.claims))
    parts = [
        f"Message language: {triage.lang}. Type: {triage.claim_type}. Forwarded: {msg.forwarded}. "
        f"Forwarded many times: {msg.forwarded_many_times}. Injection suspected by triage: {triage.injection_suspected}.",
        wrap_untrusted("forwarded_message", msg.combined_text() or "(no text)"),
        wrap_untrusted("extracted_claims", claims or "(none)"),
    ]
    for link in msg.links:
        parts.append(f"Link in message: {link.url} (final: {link.final_url})")
    if msg.image is not None:
        parts.append(
            "The message includes an image; reverse_image_search is available." if msg.image.phash else ""
        )
    parts.append("Investigate and finish with a verdict.")
    return "\n\n".join(p for p in parts if p)


def _resp_to_payload(r: LLMResponse) -> dict:
    u = r.usage
    return {
        "content": r.content,
        "stop_reason": r.stop_reason,
        "model": r.model,
        "usage": {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_write_tokens": u.cache_write_tokens,
        },
    }


async def journaled_llm(
    journal: Journal, llm: LLMProvider, meter: Meter, purpose: str, **kwargs
) -> LLMResponse:
    key = step_key("llm", purpose, kwargs.get("model"), len(kwargs.get("messages", [])))

    async def call():
        return _resp_to_payload(await llm.complete(purpose=purpose, **kwargs))

    payload, _ = await journal.step("llm", key, call)
    meter.add(purpose, payload, kwargs["model"])
    return LLMResponse(
        content=payload["content"],
        stop_reason=payload["stop_reason"],
        usage=Usage(**payload["usage"]),
        model=payload.get("model", ""),
    )


async def investigate(
    llm: LLMProvider,
    toolbox: Toolbox,
    journal: Journal,
    meter: Meter,
    msg: CanonicalMessage,
    triage: TriageResult,
) -> Investigation:
    s = get_settings()
    inv = Investigation()
    messages: list[dict] = [{"role": "user", "content": render_case(msg, triage)}]
    nudged = False
    context = {"msg": msg}
    while True:
        if meter.over:
            inv.stop = "budget" if meter.cost >= meter.max_cost else "steps"
            break
        meter.steps += 1
        try:
            resp = await journaled_llm(
                journal,
                llm,
                meter,
                "investigate",
                model=s.model_main,
                system=INVESTIGATE_SYSTEM,
                messages=messages,
                tools=toolbox.specs(),
                max_tokens=8000,
                effort="medium",
            )
        except LLMError as e:
            log.warning("investigation llm error: %s", e)
            inv.stop = "llm_error"
            break
        messages.append({"role": "assistant", "content": resp.content})
        uses = resp.tool_uses()
        if not uses:
            if nudged:
                inv.stop = "no_finish"
                break
            nudged = True
            messages.append({"role": "user", "content": "Call finish_investigation now with your verdict."})
            continue
        results = []
        for tu in uses:
            name, args = tu.get("name", ""), tu.get("input") or {}
            if isinstance(args, str):  # defensive: some transports hand back JSON text
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            key = step_key("tool", name, args)

            async def run(name=name, args=args):
                return (await toolbox.execute(name, args, context)).dump()

            payload, _ = await journal.step("tool", key, run)
            outcome = ToolOutcome.load(payload)
            toolbox.absorb(outcome)
            inv.tool_calls.append({"tool": name, "args": args, "error": outcome.is_error})
            if outcome.followup:
                inv.followup = outcome.followup
            if outcome.finish:
                inv.verdict = outcome.finish["verdict"]
                inv.confidence = outcome.finish["confidence"]
                inv.rationale = outcome.finish["rationale"]
                inv.finished = True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": outcome.content,
                    "is_error": outcome.is_error,
                }
            )
        if inv.finished:
            inv.stop = "finished"
            break
        messages.append({"role": "user", "content": results})
    if not inv.finished:
        inv.verdict, inv.confidence = "UNVERIFIED", 0.0
    return inv
