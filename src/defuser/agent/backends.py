"""External services used by the investigation tools. Each has a Protocol so evals and tests can swap in
frozen fixtures.

API shapes (verify against current docs when changing):
- Google Fact Check Tools: GET https://factchecktools.googleapis.com/v1alpha1/claims:search
  params query, languageCode, maxAgeDays, pageSize, key -> {claims: [{text, claimant, claimDate,
  claimReview: [{publisher: {name, site}, url, title, reviewDate, textualRating, languageCode}]}]}.
  The request and top-level response were verified from the REST reference; the Claim/ClaimReview field
  names come from the published discovery document and were not re-verified in this build.
- Brave Search: GET https://api.search.brave.com/res/v1/web/search?q=&count= with header X-Subscription-Token
  -> {web: {results: [{title, url, description}]}}. Not verified in this build (docs host unreachable).
- Google Safe Browsing v4: POST https://safebrowsing.googleapis.com/v4/threatMatches:find?key= ->
  {matches: [...]} or {}. Safe Browsing v4 is licensed for non-commercial use; commercial deployments should
  switch to the Web Risk API (same request shape at webrisk.googleapis.com/v1/uris:search, one URL per call).
- RDAP: GET https://rdap.org/domain/<domain> (bootstrap redirector) -> events[{eventAction: "registration",
  eventDate}]. Not verified in this build.
"""

from datetime import UTC, datetime
from typing import Protocol

import httpx

from defuser.config import get_settings


class FactCheckBackend(Protocol):
    async def search(self, query: str, language: str | None = None) -> list[dict]: ...


class SearchBackend(Protocol):
    async def search(self, query: str, count: int = 8) -> list[dict]: ...


class SafetyBackend(Protocol):
    async def threat_matches(self, url: str) -> list[str]: ...

    async def domain_age_days(self, domain: str) -> int | None: ...


class BackendUnavailable(Exception):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(get_settings().fetch_timeout_s))


class GoogleFactCheck:
    URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"

    def __init__(self, key: str, client: httpx.AsyncClient | None = None):
        self.key, self.client = key, client or _client()

    async def search(self, query: str, language: str | None = None) -> list[dict]:
        params = {"query": query[:500], "pageSize": 10, "key": self.key}
        if language:
            params["languageCode"] = language
        try:
            r = await self.client.get(self.URL, params=params)
        except httpx.HTTPError as e:
            raise BackendUnavailable(f"factcheck api: {type(e).__name__}") from e
        if r.status_code != 200:
            raise BackendUnavailable(f"factcheck api http {r.status_code}")
        out = []
        for c in r.json().get("claims", []):
            for rev in c.get("claimReview", []):
                out.append(
                    {
                        "claim": c.get("text", ""),
                        "claimant": c.get("claimant"),
                        "claim_date": c.get("claimDate"),
                        "publisher": (rev.get("publisher") or {}).get("name")
                        or (rev.get("publisher") or {}).get("site"),
                        "url": rev.get("url", ""),
                        "title": rev.get("title", ""),
                        "rating": rev.get("textualRating", ""),
                        "review_date": rev.get("reviewDate"),
                        "language": rev.get("languageCode"),
                    }
                )
        return out


class BraveSearch:
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, key: str, client: httpx.AsyncClient | None = None):
        self.key, self.client = key, client or _client()

    async def search(self, query: str, count: int = 8) -> list[dict]:
        try:
            r = await self.client.get(
                self.URL,
                params={"q": query[:400], "count": count},
                headers={"X-Subscription-Token": self.key, "Accept": "application/json"},
            )
        except httpx.HTTPError as e:
            raise BackendUnavailable(f"search: {type(e).__name__}") from e
        if r.status_code != 200:
            raise BackendUnavailable(f"search http {r.status_code}")
        results = (r.json().get("web") or {}).get("results") or []
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
            for x in results
        ]


class GoogleSafety:
    SB_URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find"

    def __init__(self, key: str | None, client: httpx.AsyncClient | None = None):
        self.key, self.client = key, client or _client()

    async def threat_matches(self, url: str) -> list[str]:
        if not self.key:
            raise BackendUnavailable("no GOOGLE_API_KEY")
        body = {
            "client": {"clientId": "forward-defuser", "clientVersion": "1.0"},
            "threatInfo": {
                "threatTypes": [
                    "MALWARE",
                    "SOCIAL_ENGINEERING",
                    "UNWANTED_SOFTWARE",
                    "POTENTIALLY_HARMFUL_APPLICATION",
                ],
                "platformTypes": ["ANY_PLATFORM"],
                "threatEntryTypes": ["URL"],
                "threatEntries": [{"url": url}],
            },
        }
        try:
            r = await self.client.post(self.SB_URL, params={"key": self.key}, json=body)
        except httpx.HTTPError as e:
            raise BackendUnavailable(f"safe browsing: {type(e).__name__}") from e
        if r.status_code != 200:
            raise BackendUnavailable(f"safe browsing http {r.status_code}")
        return sorted({m.get("threatType", "UNKNOWN") for m in r.json().get("matches", [])})

    async def domain_age_days(self, domain: str) -> int | None:
        try:
            r = await self.client.get(f"https://rdap.org/domain/{domain}", follow_redirects=True)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        for ev in r.json().get("events", []):
            if ev.get("eventAction") == "registration" and ev.get("eventDate"):
                when = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                return (datetime.now(UTC) - when).days
        return None


class Unconfigured:
    """Stands in when a key is missing: the tool reports itself unavailable instead of crashing the run."""

    def __init__(self, what: str):
        self.what = what

    async def search(self, *a, **k):
        raise BackendUnavailable(f"{self.what} not configured")

    async def threat_matches(self, *a, **k):
        raise BackendUnavailable(f"{self.what} not configured")

    async def domain_age_days(self, *a, **k):
        return None


def default_backends() -> dict:
    s = get_settings()
    g = s.google_api_key.get_secret_value() if s.google_api_key else None
    b = s.brave_api_key.get_secret_value() if s.brave_api_key else None
    return {
        "factcheck": GoogleFactCheck(g) if g else Unconfigured("fact check api"),
        "search": BraveSearch(b) if b else Unconfigured("web search"),
        "safety": GoogleSafety(g),
    }
