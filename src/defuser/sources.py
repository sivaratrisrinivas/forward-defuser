"""Deterministic source tiering by domain."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from defuser.models import Tier

_PATH = Path(__file__).parent / "data" / "sources.yaml"
_ORDER = [Tier.SATIRE, Tier.FACTCHECKER, Tier.OFFICIAL, Tier.NEWS]


@lru_cache
def _rules() -> dict[Tier, list[tuple[str, str]]]:
    raw = yaml.safe_load(_PATH.read_text())
    out: dict[Tier, list[tuple[str, str]]] = {}
    for tier in _ORDER:
        entries = []
        for item in raw.get(tier.value, []):
            host, _, path = item.partition("/")
            entries.append((host.lower(), "/" + path if path else ""))
        out[tier] = entries
    return out


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def tier_for(url: str) -> Tier:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path or "/"
    for tier in _ORDER:
        for dom, prefix in _rules()[tier]:
            if domain_matches(host, dom) and (not prefix or path.startswith(prefix)):
                return tier
    return Tier.OTHER


def publisher_for(url: str) -> str:
    host = host_of(url)
    return host[4:] if host.startswith("www.") else host
