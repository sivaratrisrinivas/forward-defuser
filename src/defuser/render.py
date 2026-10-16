"""Turns a verified draft into the final chat message using per-language templates."""

from defuser.i18n import pick, t
from defuser.models import Draft, Evidence, Verdict


def render_reply(
    draft: Draft, evidence: dict[str, Evidence], lang: str, seed: str, truncated: bool = False
) -> str:
    v = draft.verdict.value
    lines = [t(lang, "headline", v)]
    opener = pick(lang, "opener", v, seed)
    if opener:
        lines.append(opener)
    lines.append("")
    # One numbered source per URL: a fact-check record and the fetched article often share a link.
    order: list[str] = []
    first_ev: dict[str, str] = {}
    for reason in draft.reasons:
        refs = []
        for c in reason.citations:
            url = evidence[c.evidence_id].url
            if url not in order:
                order.append(url)
                first_ev[url] = c.evidence_id
            refs.append(order.index(url) + 1)
        marks = "".join(f"[{n}]" for n in sorted(set(refs)))
        lines.append(f"• {reason.text.strip()} {marks}")
    if order:
        lines.append("")
        lines.append(t(lang, "sources"))
        for n, url in enumerate(order, 1):
            ev = evidence[first_ev[url]]
            lines.append(f"[{n}] {ev.publisher or ''} {url}".replace("  ", " "))
    lines.append("")
    lines.append(t(lang, "next_step", v))
    if truncated:
        lines.append(t(lang, "too_long"))
    out = "\n".join(lines).strip()
    while "\n\n\n" in out:  # no reasons: don't leave a double gap
        out = out.replace("\n\n\n", "\n\n")
    return out


def render_simple(lang: str, key: str) -> str:
    return t(lang, key)


def render_opinion(lang: str) -> str:
    return t(lang, "opinion")


def verdict_headline(lang: str, verdict: Verdict) -> str:
    return t(lang, "headline", verdict.value)
