"""One fact-check of a canonical message: triage -> investigate -> compose -> verify -> render.

Used by the live pipeline, follow-up re-checks and the eval harness (which passes pool=None and frozen
backends)."""

import logging
import time
import uuid
from dataclasses import dataclass, field

import asyncpg

from defuser.agent.journal import Journal
from defuser.agent.loop import Investigation, investigate
from defuser.agent.meter import Meter
from defuser.agent.tools import Toolbox
from defuser.composer import compose
from defuser.config import get_settings
from defuser.i18n import resolve
from defuser.llm.provider import LLMProvider
from defuser.models import (
    CanonicalMessage,
    ClaimType,
    Draft,
    Evidence,
    Tier,
    TriageResult,
    Verdict,
    VerifierReport,
)
from defuser.normalize.fetch import Fetcher
from defuser.observability import VERIFIER, tracer
from defuser.render import render_opinion, render_reply, render_simple
from defuser.sources import publisher_for, tier_for
from defuser.triage import triage

log = logging.getLogger(__name__)

HIGH_STAKES_MIN_CONFIDENCE = 0.75


@dataclass
class CheckResult:
    run_id: str
    kind: str  # checked | opinion | nothing | budget
    verdict: Verdict
    confidence: float
    reply: str
    lang: str
    triage: TriageResult | None = None
    investigation: Investigation | None = None
    draft: Draft | None = None
    evidence: dict[str, Evidence] = field(default_factory=dict)
    report: VerifierReport | None = None
    compose_attempts: int = 0
    meter: Meter | None = None
    needs_review: str | None = None
    followup: bool = False
    latency_ms: int = 0
    replayed_steps: int = 0

    def trace(self) -> dict:
        return {
            "triage": self.triage.model_dump() if self.triage else None,
            "tool_calls": self.investigation.tool_calls if self.investigation else [],
            "investigator": {
                "verdict": self.investigation.verdict,
                "confidence": self.investigation.confidence,
                "stop": self.investigation.stop,
                "rationale": self.investigation.rationale,
            }
            if self.investigation
            else None,
            "draft": self.draft.model_dump() if self.draft else None,
            "verifier": self.report.model_dump() if self.report else None,
            "compose_attempts": self.compose_attempts,
            "llm_calls": self.meter.calls if self.meter else [],
            "evidence": [
                {
                    "id": e.evidence_id,
                    "url": e.url,
                    "tier": e.tier.value,
                    "label": e.label.value if e.label else None,
                    "kind": e.kind,
                }
                for e in self.evidence.values()
            ],
            "replayed_steps": self.replayed_steps,
            "needs_review": self.needs_review,
        }


class Checker:
    def __init__(
        self,
        llm: LLMProvider,
        backends: dict,
        fetcher: Fetcher | None = None,
        pool: asyncpg.Pool | None = None,
        image_backend=None,
    ):
        self.llm, self.backends, self.pool = llm, backends, pool
        self.fetcher = fetcher or Fetcher()
        self.image_backend = image_backend

    async def check(
        self, msg: CanonicalMessage, run_id: str | None = None, max_cost: float | None = None
    ) -> CheckResult:
        s = get_settings()
        run_id = run_id or str(uuid.uuid4())
        t0 = time.monotonic()
        journal = await Journal(self.pool, run_id).load()
        meter = Meter(
            max_cost=max_cost if max_cost is not None else s.max_cost_per_message_usd,
            max_steps=s.max_steps_per_message,
        )
        with tracer().start_as_current_span("check") as span:
            span.set_attribute("run_id", run_id)
            tri = await triage(self.llm, journal, meter, msg)
            lang = resolve(tri.lang if tri.lang else msg.lang)
            span.set_attribute("lang", lang)
            if not tri.claims:
                return self._done(
                    CheckResult(
                        run_id,
                        "nothing",
                        Verdict.UNVERIFIED,
                        0.0,
                        render_simple(lang, "nothing_to_check"),
                        lang,
                        tri,
                        meter=meter,
                    ),
                    t0,
                    journal,
                )
            if not tri.checkable or tri.claim_type == ClaimType.OPINION:
                return self._done(
                    CheckResult(
                        run_id,
                        "opinion",
                        Verdict.UNVERIFIED,
                        0.0,
                        render_opinion(lang),
                        lang,
                        tri,
                        meter=meter,
                    ),
                    t0,
                    journal,
                )

            toolbox = Toolbox(
                self.backends, self.fetcher, image_backend=self.image_backend if msg.image else None
            )
            self._preload_message_links(toolbox, msg)
            inv = await investigate(self.llm, toolbox, journal, meter, msg, tri)
            span.set_attribute("investigation.stop", inv.stop)

            if inv.stop == "llm_error" and not toolbox.evidence:
                draft, report, attempts = Draft(), VerifierReport(passed=True), 0
            else:
                # The composer gets a small extra allowance so a run that spent its budget investigating can still
                # explain what it found; the hard cap is enforced again by the daily budgets.
                meter.max_cost = meter.max_cost * 1.5
                draft, report, attempts = await compose(
                    self.llm, journal, meter, tri, inv, toolbox.evidence, lang
                )
            VERIFIER.labels("pass" if report.passed else "fail").inc()

            res = CheckResult(
                run_id,
                "checked",
                draft.verdict,
                draft.confidence,
                "",
                lang,
                tri,
                inv,
                draft,
                dict(toolbox.evidence),
                report,
                attempts,
                meter,
            )
            self._apply_policy(res)
            res.followup = res.verdict == Verdict.UNVERIFIED or bool(
                inv.followup and res.verdict == Verdict.UNVERIFIED
            )
            res.reply = render_reply(res.draft, res.evidence, lang, seed=run_id, truncated=msg.truncated)
            span.set_attribute("verdict", res.verdict.value)
            return self._done(res, t0, journal)

    def _preload_message_links(self, toolbox: Toolbox, msg: CanonicalMessage) -> None:
        from defuser.agent.tools import ToolOutcome

        evs = []
        for link in msg.links:
            if link.error or len(link.text) < 80:
                continue
            evs.append(
                Evidence(
                    evidence_id=f"E{len(evs) + 1}",
                    url=link.url,
                    final_url=link.final_url,
                    title=link.title,
                    publisher=publisher_for(link.final_url),
                    tier=tier_for(link.final_url),
                    kind="page",
                    text=link.text,
                )
            )
        toolbox.absorb(ToolOutcome(content="", evidence=evs))

    def _apply_policy(self, res: CheckResult) -> None:
        """Precision rules that sit on top of the verifier."""
        d = res.draft
        assert d is not None
        reason = None
        if d.verdict in (Verdict.FALSE, Verdict.SCAM) and d.confidence < HIGH_STAKES_MIN_CONFIDENCE:
            reason = f"low-confidence {d.verdict.value} ({d.confidence:.2f}) downgraded"
        elif d.verdict == Verdict.TRUE and res.triage and res.triage.injection_suspected:
            reason = "TRUE on a message with suspected prompt injection downgraded"
        elif (
            d.verdict == Verdict.TRUE
            and not any(e.tier in (Tier.OFFICIAL, Tier.FACTCHECKER) for e in res.evidence.values())
            and d.confidence < 0.9
        ):
            reason = "TRUE without a strong source, sent for review"
        if reason:
            res.needs_review = reason
            res.draft = Draft(verdict=Verdict.UNVERIFIED, confidence=d.confidence, reasons=[])
            res.verdict, res.confidence = Verdict.UNVERIFIED, d.confidence
        elif res.report and not res.report.passed:
            res.needs_review = "verifier rejected both drafts"
        elif (
            res.investigation
            and d.verdict != Verdict.UNVERIFIED
            and res.investigation.verdict != d.verdict.value
        ):
            res.needs_review = f"composer changed verdict {res.investigation.verdict} -> {d.verdict.value}"

    def _done(self, res: CheckResult, t0: float, journal: Journal) -> CheckResult:
        res.latency_ms = int((time.monotonic() - t0) * 1000)
        res.replayed_steps = journal.replayed
        if res.meter is None:
            res.meter = Meter(0, 0)
        return res
