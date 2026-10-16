"""Drafts the cited reasons with the main model, then runs the verifier; regenerates once on failure."""

import json
import logging

from defuser.agent.journal import Journal
from defuser.agent.loop import Investigation, journaled_llm
from defuser.agent.meter import Meter
from defuser.agent.prompts import COMPOSE_SYSTEM
from defuser.agent.tools import wrap_untrusted
from defuser.config import get_settings
from defuser.i18n import language_name
from defuser.llm.provider import LLMError, LLMProvider
from defuser.models import Draft, Evidence, Reason, TriageResult, Verdict, VerifierReport
from defuser.verifier import verify

log = logging.getLogger(__name__)

COMPOSE_TEXT_CHARS = 6000

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": [v.value for v in Verdict]},
        "confidence": {"type": "number"},
        "reasons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"evidence_id": {"type": "string"}, "quote": {"type": "string"}},
                            "required": ["evidence_id", "quote"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["text", "citations"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "confidence", "reasons"],
    "additionalProperties": False,
}


def _evidence_block(evidence: dict[str, Evidence]) -> str:
    blocks = []
    for ev in evidence.values():
        blocks.append(
            wrap_untrusted(
                "source",
                ev.text[:COMPOSE_TEXT_CHARS],
                id=ev.evidence_id,
                tier=ev.tier.value,
                label=ev.label.value if ev.label else "unlabelled",
                publisher=ev.publisher or "",
            )
        )
    return "\n".join(blocks) or "(no evidence was found)"


async def compose(
    llm: LLMProvider,
    journal: Journal,
    meter: Meter,
    triage: TriageResult,
    inv: Investigation,
    evidence: dict[str, Evidence],
    lang: str,
) -> tuple[Draft, VerifierReport, int]:
    """Returns (draft, report, attempts). On a second failure returns an UNVERIFIED draft with no reasons."""
    s = get_settings()
    claims = "\n".join(f"- {c.canonical_en}" for c in triage.claims)
    base = (
        f"Write in: {language_name(lang)} (code {lang}).\n"
        f"Investigator verdict: {inv.verdict} (confidence {inv.confidence:.2f}).\n"
        f"Investigator rationale: {wrap_untrusted('rationale', inv.rationale)}\n"
        f"Claims checked:\n{wrap_untrusted('claims', claims)}\n\nEvidence:\n{_evidence_block(evidence)}"
    )
    messages: list[dict] = [{"role": "user", "content": base}]
    report = VerifierReport(passed=False)
    for attempt in (1, 2):
        try:
            resp = await journaled_llm(
                journal,
                llm,
                meter,
                f"compose{attempt}",
                model=s.model_main,
                system=COMPOSE_SYSTEM,
                messages=messages,
                json_schema=DRAFT_SCHEMA,
                max_tokens=3000,
                effort="low",
            )
            data = json.loads(resp.text())
            draft = Draft(
                verdict=Verdict(data["verdict"]),
                confidence=max(0.0, min(1.0, float(data.get("confidence", 0)))),
                reasons=[Reason(**r) for r in data.get("reasons", [])],
            )
        except (LLMError, ValueError, KeyError, TypeError) as e:
            log.warning("compose attempt %d failed: %s", attempt, e)
            report = VerifierReport(
                passed=False, errors=[{"code": "malformed_output", "detail": str(e)[:200]}]
            )
            if attempt == 1:
                continue
            break
        report = verify(draft, evidence)
        if report.passed:
            return draft, report, attempt
        if attempt == 1:
            feedback = "\n".join(f"- {e.code}: {e.detail}" for e in report.errors)
            messages += [
                {"role": "assistant", "content": resp.content},
                {
                    "role": "user",
                    "content": "A deterministic checker rejected that draft:\n"
                    f"{feedback}\nFix every problem. Quotes must be copied exactly from the evidence text. "
                    "If the evidence cannot support the verdict, use UNVERIFIED.",
                },
            ]
    return Draft(verdict=Verdict.UNVERIFIED, confidence=0.0, reasons=[]), report, 2
