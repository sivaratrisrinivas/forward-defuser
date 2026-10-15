"""Claim extraction, type classification and language detection with the cheap model."""

import json
import logging

from defuser.agent.journal import Journal
from defuser.agent.loop import journaled_llm
from defuser.agent.meter import Meter
from defuser.agent.prompts import TRIAGE_SYSTEM
from defuser.agent.tools import wrap_untrusted
from defuser.config import get_settings
from defuser.llm.provider import LLMError, LLMProvider
from defuser.models import CanonicalMessage, Claim, ClaimType, TriageResult

log = logging.getLogger(__name__)

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "canonical_en": {"type": "string"}},
                "required": ["text", "canonical_en"],
                "additionalProperties": False,
            },
        },
        "claim_type": {"type": "string", "enum": [t.value for t in ClaimType]},
        "checkable": {"type": "boolean"},
        "lang": {"type": "string"},
        "injection_suspected": {"type": "boolean"},
    },
    "required": ["claims", "claim_type", "checkable", "lang", "injection_suspected"],
    "additionalProperties": False,
}


async def triage(llm: LLMProvider, journal: Journal, meter: Meter, msg: CanonicalMessage) -> TriageResult:
    s = get_settings()
    content = wrap_untrusted("forwarded_message", msg.combined_text() or "(empty)")
    if msg.links:
        content += "\nLinks: " + ", ".join(link.final_url for link in msg.links)
    try:
        resp = await journaled_llm(
            journal,
            llm,
            meter,
            "triage",
            model=s.model_cheap,
            system=TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            json_schema=TRIAGE_SCHEMA,
            max_tokens=2000,
            effort="low",
        )
        data = json.loads(resp.text())
        res = TriageResult(
            claims=[Claim(**c) for c in data["claims"][:3]],
            claim_type=ClaimType(data["claim_type"])
            if data["claim_type"] in ClaimType._value2member_map_
            else ClaimType.OTHER,
            checkable=bool(data["checkable"]),
            lang=(data.get("lang") or msg.lang).lower(),
            injection_suspected=bool(data.get("injection_suspected")),
        )
    except (LLMError, ValueError, KeyError, TypeError) as e:
        log.warning("triage failed, falling back: %s", e)
        text = msg.combined_text()[:500]
        res = TriageResult(claims=[Claim(text=text, canonical_en=text)] if text else [], lang=msg.lang)
    # Keep the script-based guess when the model returns something we don't support.
    if res.lang not in {"en", "hi", "hinglish", "ta", "te", "bn", "mr"}:
        res.lang = msg.lang
    if msg.lang == "hinglish" and res.lang == "en":
        res.lang = "hinglish"
    return res
