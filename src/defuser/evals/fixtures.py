"""Frozen backends: every external call is answered from a case's fixture data. Used by tests and offline /
live-model evals so results are reproducible and no network is needed."""

import asyncio
import random

from defuser.agent.backends import BackendUnavailable
from defuser.normalize.fetch import FetchResult


class FixtureFactCheck:
    def __init__(self, records: list[dict], fail: str | None = None, delay: float = 0.0):
        self.records, self.fail, self.delay = records, fail, delay
        self.queries: list[str] = []

    async def search(self, query, language=None):
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail == "down":
            raise BackendUnavailable("fact check api http 503")
        if self.fail == "junk":
            return [{"url": "", "claim": "\x00\x00", "rating": None}]
        return [dict(r) for r in self.records]


class FixtureSearch:
    def __init__(self, results: list[dict], fail: str | None = None, delay: float = 0.0):
        self.results, self.fail, self.delay = results, fail, delay

    async def search(self, query, count=8):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail == "down":
            raise BackendUnavailable("search http 503")
        if self.fail == "junk":
            return [{"title": "<<<>>>", "url": "javascript:alert(1)", "snippet": "�" * 50}]
        # Honour "site:" filters so official lookups only see official results.
        sites = [
            p.split("site:")[1].strip("() ")
            for p in query.split()
            if p.startswith("(site:") or p.startswith("site:")
        ]
        res = self.results
        if sites:
            from defuser.sources import domain_matches, host_of

            res = [r for r in res if any(domain_matches(host_of(r["url"]), s) for s in sites)]
        return [dict(r) for r in res][:count]


class FixtureSafety:
    def __init__(
        self,
        threats: dict[str, list[str]] | None = None,
        ages: dict[str, int] | None = None,
        fail: str | None = None,
    ):
        self.threats, self.ages, self.fail = threats or {}, ages or {}, fail

    async def threat_matches(self, url):
        if self.fail == "down":
            raise BackendUnavailable("safe browsing http 503")
        for k, v in self.threats.items():
            if k in url:
                return v
        return []

    async def domain_age_days(self, domain):
        return self.ages.get(domain)


class FixtureFetcher:
    """Same interface as normalize.fetch.Fetcher."""

    def __init__(
        self,
        pages: dict[str, str],
        redirects: dict[str, str] | None = None,
        fail: str | None = None,
        delay: float = 0.0,
    ):
        self.pages, self.redirects, self.fail, self.delay = pages, redirects or {}, fail, delay
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail == "down":
            return FetchResult(url=url, final_url=url, error="ConnectError")
        if self.fail == "flaky" and random.random() < 0.5:
            return FetchResult(url=url, final_url=url, error="ReadTimeout")
        chain, final = [], url
        while final in self.redirects and len(chain) < 5:
            chain.append(final)
            final = self.redirects[final]
        body = self.pages.get(final)
        if body is None:
            return FetchResult(url=url, final_url=final, redirect_chain=chain, status=404, error="http 404")
        if self.fail == "junk":
            body = "�" * 500
        return FetchResult(
            url=url,
            final_url=final,
            status=200,
            content_type="text/plain",
            body=body.encode(),
            redirect_chain=chain,
        )


def backends_from_case(fx: dict, fail: dict | None = None) -> tuple[dict, FixtureFetcher]:
    fail = fail or {}
    backends = {
        "factcheck": FixtureFactCheck(fx.get("factchecks", []), fail.get("factcheck")),
        "search": FixtureSearch(fx.get("search", []), fail.get("search")),
        "safety": FixtureSafety(fx.get("safety", {}), fx.get("domain_age", {}), fail.get("safety")),
    }
    fetcher = FixtureFetcher(fx.get("pages", {}), fx.get("redirects", {}), fail.get("fetch"))
    return backends, fetcher
