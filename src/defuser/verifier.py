"""Deterministic checks on a draft before anything reaches a user. No LLM involved.

A draft passes only if:
- every reason cites at least one evidence item fetched during this run, and every quote is a verbatim
  substring (after Unicode NFC and whitespace normalisation only) of that evidence's stored text;
- quotes are long enough to carry meaning, and any number in a reason appears in that reason's quotes;
- the verdict is backed by labelled evidence of a sufficient tier among the cited sources;
- reasons contain no URLs, phone numbers or instruction-like text (prompt-injection leakage).
"""

import re
import unicodedata

from defuser.models import Draft, Evidence, Label, Tier, Verdict, VerifierError, VerifierReport

MIN_QUOTE_WORDS = 6
MIN_QUOTE_CHARS = 25
MAX_REASONS = 3
STRONG = {Tier.FACTCHECKER, Tier.OFFICIAL}

LEAK_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above)",
    r"system prompt",
    r"untrusted_",
    r"as an ai\b",
    r"language model",
    r"\byou are now\b",
    r"forward (this|it) to",
    r"share (this|it) with",
    r"click (here|the link|this)",
    r"\bOTP\b.*\b(share|send|bhej)",
    r"<\/?[a-z_]+>",
]
URL_RE = re.compile(r"(?i)(https?://|www\.)\S+|\b[a-z0-9-]+\.(com|in|org|net|xyz|top|info|ly|co)\b(/\S*)?")
PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s-]{6,}\d)(?!\d)")
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
TRUEISH_RATING = re.compile(r"(?i)^\s*(true|correct|accurate|mostly true|सच|सही)\b")
FALSEISH_RATING = re.compile(r"(?i)\b(false|fake|misleading|incorrect|scam|hoax|झूठ|फर्जी|भ्रामक)\b")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text.translate(DEVANAGARI_DIGITS))}


def _rating(ev: Evidence) -> str | None:
    m = re.search(r"Rating: (.*?)\. Title:", ev.text)
    return m.group(1) if m else None


def _err(code: str, detail: str) -> VerifierError:
    return VerifierError(code=code, detail=detail)


def _verdict_support(verdict: Verdict, cited: list[Evidence], all_ev: list[Evidence]) -> str | None:
    """Return an error detail if the verdict isn't backed by the cited evidence."""

    def has(labels: set[Label], tiers: set[Tier]) -> bool:
        return any(e.label in labels and e.tier in tiers for e in cited)

    def distinct_publishers(label: Label, tier: Tier) -> int:
        return len({e.publisher for e in cited if e.label == label and e.tier == tier})

    strong_support = any(e.label == Label.SUPPORTS and e.tier in STRONG for e in all_ev)
    strong_refute = any(e.label == Label.REFUTES and e.tier in STRONG for e in all_ev)
    ratings = [_rating(e) for e in all_ev if e.kind == "factcheck_record" and e.tier in STRONG]
    # Fact-check records come from searching this claim; a debunk rating anywhere blocks TRUE, and a "true"
    # rating anywhere blocks FALSE. Precision over recall.
    if verdict == Verdict.TRUE and any(r and FALSEISH_RATING.search(r) for r in ratings):
        return "a fact-checker rated a matching claim false/misleading"
    if verdict == Verdict.FALSE and any(
        r and TRUEISH_RATING.search(r) and not FALSEISH_RATING.search(r) for r in ratings
    ):
        return "a fact-checker rated a matching claim true"
    if verdict == Verdict.FALSE:
        if strong_support:
            return "a strong source supports the claim"
        if not (has({Label.REFUTES}, STRONG) or distinct_publishers(Label.REFUTES, Tier.NEWS) >= 2):
            return "FALSE needs a cited fact-checker/official refutation or two independent news refutations"
    elif verdict == Verdict.TRUE:
        if strong_refute:
            return "a strong source refutes the claim"
        if not (has({Label.SUPPORTS}, STRONG) or distinct_publishers(Label.SUPPORTS, Tier.NEWS) >= 2):
            return (
                "TRUE needs a cited fact-checker/official confirmation or two independent news confirmations"
            )
    elif verdict == Verdict.MISLEADING:
        if not has({Label.REFUTES, Label.CONTEXT}, STRONG):
            return "MISLEADING needs cited fact-checker/official context or refutation"
    elif verdict == Verdict.SATIRE:
        if not has({Label.SATIRE_SIGNAL}, {Tier.SATIRE, Tier.FACTCHECKER}):
            return "SATIRE needs a cited satire-site or fact-checker satire signal"
    elif verdict == Verdict.SCAM:
        if not has({Label.SCAM_SIGNAL}, STRONG):
            return "SCAM needs a cited safety-list hit, official advisory or fact-checker scam finding"
    return None


def verify(draft: Draft, evidence: dict[str, Evidence]) -> VerifierReport:
    errors: list[VerifierError] = []
    if len(draft.reasons) > MAX_REASONS:
        errors.append(_err("too_many_reasons", str(len(draft.reasons))))
    if draft.verdict != Verdict.UNVERIFIED and not draft.reasons:
        errors.append(_err("no_reasons", "a non-UNVERIFIED verdict needs cited reasons"))

    cited: list[Evidence] = []
    for i, reason in enumerate(draft.reasons, 1):
        if not reason.text.strip():
            errors.append(_err("empty_reason", f"reason {i}"))
            continue
        if not reason.citations:
            errors.append(_err("uncited_reason", f"reason {i} has no citation"))
        quotes_text = ""
        for c in reason.citations:
            ev = evidence.get(c.evidence_id)
            if ev is None:
                errors.append(
                    _err("unknown_evidence", f"reason {i} cites {c.evidence_id}, not fetched in this run")
                )
                continue
            q = norm(c.quote)
            if len(q) < MIN_QUOTE_CHARS or len(q.split()) < MIN_QUOTE_WORDS:
                errors.append(_err("quote_too_short", f"reason {i}: {q!r}"))
            if q not in norm(ev.text):
                errors.append(_err("quote_not_found", f"reason {i}: quote not verbatim in {c.evidence_id}"))
            else:
                cited.append(ev)
                quotes_text += " " + q
            rating = _rating(ev) if ev.kind == "factcheck_record" else None
            if (
                rating
                and ev.label == Label.REFUTES
                and TRUEISH_RATING.search(rating)
                and not FALSEISH_RATING.search(rating)
            ):
                errors.append(_err("label_contradicts_rating", f"{c.evidence_id} rated {rating!r}"))
            if rating and ev.label == Label.SUPPORTS and FALSEISH_RATING.search(rating):
                errors.append(_err("label_contradicts_rating", f"{c.evidence_id} rated {rating!r}"))
        missing = numbers_in(reason.text) - numbers_in(quotes_text)
        if missing:
            errors.append(_err("number_not_in_quote", f"reason {i}: {sorted(missing)}"))
        low = reason.text.lower()
        for pat in LEAK_PATTERNS:
            if re.search(pat, low, flags=re.I):
                errors.append(_err("instruction_leak", f"reason {i} matches {pat!r}"))
        if URL_RE.search(reason.text):
            errors.append(_err("foreign_url", f"reason {i} contains a link"))
        phones = [
            m
            for m in PHONE_RE.findall(reason.text.translate(DEVANAGARI_DIGITS))
            if sum(ch.isdigit() for ch in m) >= 10
        ]
        if phones:
            errors.append(_err("foreign_phone", f"reason {i} contains a phone number"))

    problem = _verdict_support(draft.verdict, cited, list(evidence.values()))
    if problem:
        errors.append(_err("verdict_inconsistent", problem))
    return VerifierReport(passed=not errors, errors=errors)
