"""reverse_image_search: where else has this image appeared? Backed by the web-detection results computed when
the image was normalised; pages from fact-checkers / news / official sites are fetched as evidence."""

from defuser.agent.tools import ToolOutcome, _schema, wrap_untrusted
from defuser.models import Tier
from defuser.sources import tier_for

REVERSE_IMAGE_SPEC = {
    "name": "reverse_image_search",
    "description": "Find earlier appearances of the image in the message (useful for old photos shared as new). "
    "Pages from fact-checkers and news sites are fetched and saved as evidence.",
    "input_schema": _schema({}, []),
    "strict": True,
}

PREFERRED = (Tier.FACTCHECKER, Tier.OFFICIAL, Tier.NEWS)


async def reverse_image_search(toolbox, context) -> ToolOutcome:
    msg = (context or {}).get("msg")
    image = getattr(msg, "image", None)
    if image is None:
        return ToolOutcome(content="There is no image in this message.", is_error=True)
    if not image.matching_pages and not image.web_entities:
        return ToolOutcome(content="No earlier appearances of this image were found.")
    lines = [
        f"- [{tier_for(p['url']).value}] {p.get('title', '')} | {p['url']}" for p in image.matching_pages
    ]
    if image.web_entities:
        lines.append("Web entities: " + ", ".join(image.web_entities))
    out = [wrap_untrusted("image_matches", "\n".join(lines))]
    picks = sorted(image.matching_pages, key=lambda p: tier_for(p["url"]) not in PREFERRED)[:2]
    evs = []
    for p in picks:
        if tier_for(p["url"]) not in PREFERRED:
            continue
        ev, m = await toolbox._fetch_as_evidence(p["url"], f"E{len(toolbox.evidence) + len(evs) + 1}")
        if ev:
            ev.kind = "image_match"
            evs.append(ev)
        out.append(m)
    return ToolOutcome(content="\n\n".join(out), evidence=evs)
