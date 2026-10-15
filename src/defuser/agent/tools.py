"""Investigation tools. Every tool returns a ToolOutcome: text for the model (untrusted content wrapped in
delimited data blocks) plus any Evidence created. Outcomes are journaled so a resumed run replays them."""

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from defuser.agent.backends import BackendUnavailable
from defuser.config import get_settings
from defuser.models import Evidence, Label, Tier, Verdict
from defuser.normalize.fetch import Fetcher, extract_text
from defuser.observability import TOOL_CALLS
from defuser.sources import publisher_for, tier_for

MAX_EVIDENCE_CHARS = 20_000
EXCERPT_CHARS = 3_500

OFFICIAL_SITES = {
    "pib": ["pib.gov.in"],
    "who": ["who.int"],
    "rbi": ["rbi.org.in"],
    "health_ministry": ["mohfw.gov.in", "icmr.gov.in"],
    "election_commission": ["eci.gov.in"],
    "payments": ["npci.org.in"],
    "aadhaar": ["uidai.gov.in"],
    "cybercrime": ["cybercrime.gov.in"],
    "any_gov": ["gov.in", "nic.in"],
}


def _strip_tags(text: str) -> str:
    return re.sub(r"(?i)</?\s*(untrusted_[a-z_]+|system|instructions?)[^>]*>", " ", text)


def wrap_untrusted(tag: str, body: str, **attrs: str) -> str:
    a = " ".join(f'{k}="{_strip_tags(str(v)).replace(chr(34), "")}"' for k, v in attrs.items())
    return f"<untrusted_{tag} {a}>\n{_strip_tags(body)}\n</untrusted_{tag}>"


@dataclass
class ToolOutcome:
    content: str
    evidence: list[Evidence] = field(default_factory=list)
    label_updates: dict[str, str] = field(default_factory=dict)
    followup: str | None = None
    finish: dict | None = None
    is_error: bool = False

    def dump(self) -> dict:
        return {
            "content": self.content,
            "evidence": [e.model_dump() for e in self.evidence],
            "label_updates": self.label_updates,
            "followup": self.followup,
            "finish": self.finish,
            "is_error": self.is_error,
        }

    @classmethod
    def load(cls, d: dict) -> "ToolOutcome":
        return cls(
            content=d["content"],
            evidence=[Evidence(**e) for e in d["evidence"]],
            label_updates=d["label_updates"],
            followup=d["followup"],
            finish=d["finish"],
            is_error=d["is_error"],
        )


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


VERDICTS = [v.value for v in Verdict]

TOOL_SPECS: list[dict] = [
    {
        "name": "search_factchecks",
        "description": "Search published fact-checks (Google Fact Check Tools: IFCN signatories incl. Alt News, BOOM, "
        "Factly, Vishvas News, Newschecker, PIB Fact Check). Results are saved as evidence. Query in English and, if "
        "useful, again in the message's language.",
        "input_schema": _schema(
            {
                "query": {"type": "string"},
                "language": {"type": "string", "description": "BCP-47 like en or hi"},
            },
            ["query", "language"],
        ),
    },
    {
        "name": "web_search",
        "description": "General web search. Returns titles, URLs and snippets. Snippets are NOT evidence: call fetch_url on "
        "a result before relying on it.",
        "input_schema": _schema({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "fetch_url",
        "description": "Fetch a web page (sandboxed). The page text is stored as evidence and you get an excerpt plus its "
        "evidence id. Only fetched pages can be cited.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    },
    {
        "name": "lookup_official_source",
        "description": "Search an official authority's site and fetch the top matching pages as evidence.",
        "input_schema": _schema(
            {"query": {"type": "string"}, "authority": {"type": "string", "enum": sorted(OFFICIAL_SITES)}},
            ["query", "authority"],
        ),
    },
    {
        "name": "check_url_safety",
        "description": "Check a link from the forwarded message: Google Safe Browsing threat lists, domain age, and the "
        "redirect chain. Use for any link in a suspected scam.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    },
    {
        "name": "save_evidence",
        "description": "Label a piece of evidence by id: supports or refutes the claim, gives context, shows a scam "
        "signal, or shows the source is satire.",
        "input_schema": _schema(
            {
                "evidence_id": {"type": "string"},
                "label": {"type": "string", "enum": [label.value for label in Label]},
                "note": {"type": "string"},
            },
            ["evidence_id", "label", "note"],
        ),
    },
    {
        "name": "schedule_followup",
        "description": "Ask to re-check this claim later (6h, 24h, 72h) because coverage may appear. Use when the claim is "
        "new or evidence is thin.",
        "input_schema": _schema({"reason": {"type": "string"}}, ["reason"]),
    },
    {
        "name": "finish_investigation",
        "description": "End the investigation with your verdict. UNVERIFIED is the default whenever evidence is not "
        "strong. FALSE and SCAM need a fact-checker, official source or safety-list hit that you labelled.",
        "input_schema": _schema(
            {
                "verdict": {"type": "string", "enum": VERDICTS},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
            },
            ["verdict", "confidence", "rationale"],
        ),
    },
]
for _spec in TOOL_SPECS:
    _spec["strict"] = True


class Toolbox:
    def __init__(self, backends: dict, fetcher: Fetcher | None = None, image_backend=None):
        self.backends = backends
        self.fetcher = fetcher or Fetcher()
        self.image_backend = image_backend
        self.evidence: dict[str, Evidence] = {}
        self.fetched_urls: dict[str, str] = {}

    # ---- evidence bookkeeping (also used on journal replay) ----
    def next_id(self) -> str:
        return f"E{len(self.evidence) + 1}"

    def absorb(self, outcome: ToolOutcome) -> None:
        for ev in outcome.evidence:
            self.evidence[ev.evidence_id] = ev
            if ev.kind == "page":
                self.fetched_urls.setdefault(ev.final_url, ev.evidence_id)
                self.fetched_urls.setdefault(ev.url, ev.evidence_id)
        for eid, label in outcome.label_updates.items():
            if eid in self.evidence:
                self.evidence[eid].label = Label(label)

    def specs(self) -> list[dict]:
        specs = list(TOOL_SPECS)
        if self.image_backend is not None:
            from defuser.agent.image_tools import REVERSE_IMAGE_SPEC

            specs.insert(4, REVERSE_IMAGE_SPEC)
        return specs

    async def execute(self, name: str, args: dict, context: dict) -> ToolOutcome:
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            return ToolOutcome(content=f"Unknown tool {name}", is_error=True)
        try:
            out = await fn(context=context, **args)
            TOOL_CALLS.labels(name, "true").inc()
        except BackendUnavailable as e:
            TOOL_CALLS.labels(name, "false").inc()
            out = ToolOutcome(
                content=f"Tool unavailable right now ({e}). Try another tool or finish.", is_error=True
            )
        except TypeError as e:
            out = ToolOutcome(content=f"Bad arguments: {e}", is_error=True)
        return out

    # ---- tools ----
    async def _t_search_factchecks(self, query: str, language: str = "", context=None) -> ToolOutcome:
        results = await self.backends["factcheck"].search(query, language or None)
        if not results:
            return ToolOutcome(content="No published fact-checks matched this query.")
        new: list[Evidence] = []
        lines = []
        base = len(self.evidence)
        for r in results[:6]:
            if not r.get("url"):
                continue
            eid = f"E{base + len(new) + 1}"
            text = (
                f"Fact-check record. Publisher: {r.get('publisher')}. Claim reviewed: {r.get('claim')}. "
                f"Claimant: {r.get('claimant') or 'unknown'}. Rating: {r.get('rating')}. Title: {r.get('title')}. "
                f"Review date: {r.get('review_date') or 'unknown'}."
            )
            tier = tier_for(r["url"])
            new.append(
                Evidence(
                    evidence_id=eid,
                    url=r["url"],
                    final_url=r["url"],
                    title=r.get("title"),
                    publisher=r.get("publisher") or publisher_for(r["url"]),
                    tier=tier if tier != Tier.OTHER else Tier.OTHER,
                    kind="factcheck_record",
                    text=text,
                )
            )
            lines.append(wrap_untrusted("source", text, id=eid, url=r["url"], tier=tier.value))
        lines.append(
            "These are summaries from the fact-check index; fetch_url the article for fuller quotes."
        )
        return ToolOutcome(content="\n".join(lines), evidence=new)

    async def _t_web_search(self, query: str, context=None) -> ToolOutcome:
        results = await self.backends["search"].search(query)
        if not results:
            return ToolOutcome(content="No results.")
        body = "\n".join(
            f"- [{tier_for(r['url']).value}] {r['title']} | {r['url']} | {r['snippet']}" for r in results[:8]
        )
        return ToolOutcome(content=wrap_untrusted("search_results", body, query=query))

    async def _fetch_as_evidence(self, url: str, eid: str) -> tuple[Evidence | None, str]:
        if url in self.fetched_urls:
            ev = self.evidence[self.fetched_urls[url]]
            return None, f"Already fetched as {ev.evidence_id}."
        res = await self.fetcher.fetch(url)
        if not res.ok:
            return None, f"Could not fetch {url}: {res.error}"
        title, text = extract_text(res.body, res.content_type, res.final_url)
        text = (text or "").strip()
        if len(text) < 80:
            return None, f"{url} had no readable article text."
        tier = tier_for(res.final_url)
        ev = Evidence(
            evidence_id=eid,
            url=url,
            final_url=res.final_url,
            title=title,
            publisher=publisher_for(res.final_url),
            tier=tier,
            kind="page",
            text=text[:MAX_EVIDENCE_CHARS],
        )
        chain = f" (redirected via {' -> '.join(res.redirect_chain)})" if res.redirect_chain else ""
        excerpt = wrap_untrusted(
            "source", ev.text[:EXCERPT_CHARS], id=eid, url=res.final_url, tier=tier.value, title=title or ""
        )
        return ev, f"Saved as {eid}{chain}.\n{excerpt}"

    async def _t_fetch_url(self, url: str, context=None) -> ToolOutcome:
        ev, msg = await self._fetch_as_evidence(url, self.next_id())
        return ToolOutcome(
            content=msg, evidence=[ev] if ev else [], is_error=ev is None and "Could not" in msg
        )

    async def _t_lookup_official_source(self, query: str, authority: str, context=None) -> ToolOutcome:
        sites = OFFICIAL_SITES.get(authority)
        if not sites:
            return ToolOutcome(content=f"Unknown authority {authority}", is_error=True)
        site_q = " OR ".join(f"site:{s}" for s in sites)
        results = await self.backends["search"].search(f"{query} ({site_q})")
        official = [r for r in results if tier_for(r["url"]) == Tier.OFFICIAL][:2]
        if not official:
            return ToolOutcome(content=f"No official pages found on {', '.join(sites)} for this query.")
        evs, msgs = [], []
        base = len(self.evidence)
        fetched = await asyncio.gather(
            *[self._fetch_as_evidence(r["url"], f"E{base + i + 1}") for i, r in enumerate(official)]
        )
        # Re-number densely in case one fetch failed.
        for ev, msg in fetched:
            if ev:
                new_id = f"E{base + len(evs) + 1}"
                msg = msg.replace(ev.evidence_id, new_id)
                ev.evidence_id = new_id
                evs.append(ev)
            msgs.append(msg)
        return ToolOutcome(content="\n\n".join(msgs), evidence=evs)

    async def _t_check_url_safety(self, url: str, context=None) -> ToolOutcome:
        safety = self.backends["safety"]
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
        notes: list[str] = []
        threats: list[str] = []
        try:
            threats = await safety.threat_matches(url)
            notes.append(
                f"Google Safe Browsing lists {url} as: {', '.join(threats)}."
                if threats
                else f"Google Safe Browsing has no current threat listing for {url}."
            )
        except BackendUnavailable as e:
            notes.append(f"Safe Browsing check unavailable ({e}).")
        age = await safety.domain_age_days(host) if host else None
        if age is not None:
            notes.append(f"The domain {host} was registered {age} days ago.")
        res = await self.fetcher.fetch(url if "://" in url else "https://" + url)
        if res.redirect_chain:
            notes.append(f"The link redirects: {' -> '.join(res.redirect_chain + [res.final_url])}.")
        if res.error:
            notes.append(f"Fetching the link failed ({res.error}).")
        text = " ".join(notes)
        ev = Evidence(
            evidence_id=self.next_id(),
            url=f"https://transparencyreport.google.com/safe-browsing/search?url={quote(url, safe='')}",
            final_url=url,
            title=f"Link safety check for {host}",
            publisher="Google Safe Browsing" if threats else "Link check",
            tier=Tier.OFFICIAL if threats else Tier.OTHER,
            kind="safety_record",
            label=Label.SCAM_SIGNAL if threats else None,
            text=text,
        )
        return ToolOutcome(
            content=wrap_untrusted("source", text, id=ev.evidence_id, url=ev.url, tier=ev.tier.value),
            evidence=[ev],
        )

    async def _t_save_evidence(
        self, evidence_id: str, label: str, note: str = "", context=None
    ) -> ToolOutcome:
        if evidence_id not in self.evidence:
            return ToolOutcome(content=f"No evidence with id {evidence_id}.", is_error=True)
        ev = self.evidence[evidence_id]
        if ev.kind == "safety_record" and label == Label.SCAM_SIGNAL and ev.label != Label.SCAM_SIGNAL:
            return ToolOutcome(
                content="This safety record has no threat-list hit, so it cannot be labelled scam_signal.",
                is_error=True,
            )
        return ToolOutcome(content=f"{evidence_id} labelled {label}.", label_updates={evidence_id: label})

    async def _t_schedule_followup(self, reason: str, context=None) -> ToolOutcome:
        return ToolOutcome(content="Follow-up re-checks will be scheduled.", followup=reason[:300])

    async def _t_finish_investigation(
        self, verdict: str, confidence: float, rationale: str, context=None
    ) -> ToolOutcome:
        if verdict not in VERDICTS:
            return ToolOutcome(content=f"Verdict must be one of {VERDICTS}", is_error=True)
        conf = max(0.0, min(1.0, float(confidence)))
        return ToolOutcome(
            content="Investigation finished.",
            finish={"verdict": verdict, "confidence": conf, "rationale": rationale[:2000]},
        )

    async def _t_reverse_image_search(self, context=None, **_) -> ToolOutcome:
        from defuser.agent.image_tools import reverse_image_search

        return await reverse_image_search(self, context)


def max_steps() -> int:
    return get_settings().max_steps_per_message
