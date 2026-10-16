"""Sandboxed URL fetching for untrusted links.

- http/https only, standard ports only; every hop's host is resolved and rejected if it maps to a private,
  loopback, link-local, multicast or reserved address (SSRF guard), including after redirects.
- Redirects followed manually (max FETCH_MAX_REDIRECTS) so each hop is checked and the chain is recorded.
- Hard timeout and byte cap; body streamed and cut off at the cap.
- Only text/html, text/plain, application/xhtml+xml, application/xml/rss/atom are parsed.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from defuser.config import get_settings

ALLOWED_TYPES = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/json",
)
USER_AGENT = "ForwardDefuserBot/1.0 (+fact-checking; contact via bot)"


class FetchBlocked(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int = 0
    content_type: str = ""
    body: bytes = b""
    redirect_chain: list[str] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300


def _ip_allowed(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (addr.version == 6 and addr.ipv4_mapped and not _ip_allowed(str(addr.ipv4_mapped)))
    )


async def check_host(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchBlocked(f"scheme {parts.scheme!r} not allowed")
    if parts.port not in (None, 80, 443):
        raise FetchBlocked("non-standard port")
    host = parts.hostname
    if not host:
        raise FetchBlocked("no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchBlocked(f"dns failure for {host}") from e
    for info in infos:
        if not _ip_allowed(info[4][0]):
            raise FetchBlocked(f"{host} resolves to a non-public address")


class Fetcher:
    def __init__(self, client: httpx.AsyncClient | None = None, check=check_host):
        s = get_settings()
        self.timeout = s.fetch_timeout_s
        self.max_bytes = s.fetch_max_bytes
        self.max_redirects = s.fetch_max_redirects
        self._client = client or httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(self.timeout), headers={"User-Agent": USER_AGENT}
        )
        self._check = check

    async def fetch(self, url: str) -> FetchResult:
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=self.timeout * 2)
        except TimeoutError:
            return FetchResult(url=url, final_url=url, error="timeout")

    async def _fetch(self, url: str) -> FetchResult:
        chain: list[str] = []
        current = url
        for _ in range(self.max_redirects + 1):
            try:
                await self._check(current)
            except FetchBlocked as e:
                return FetchResult(url=url, final_url=current, redirect_chain=chain, error=f"blocked: {e}")
            try:
                async with self._client.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and "location" in r.headers:
                        chain.append(current)
                        current = urljoin(current, r.headers["location"])
                        continue
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    res = FetchResult(
                        url=url,
                        final_url=current,
                        status=r.status_code,
                        content_type=ctype,
                        redirect_chain=chain,
                    )
                    if ctype and not ctype.startswith(ALLOWED_TYPES):
                        res.error = f"unsupported content type {ctype}"
                        return res
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > self.max_bytes:
                            res.truncated = True
                            break
                    res.body = bytes(buf[: self.max_bytes])
                    if r.status_code >= 400:
                        res.error = f"http {r.status_code}"
                    return res
            except httpx.HTTPError as e:
                return FetchResult(
                    url=url, final_url=current, redirect_chain=chain, error=f"{type(e).__name__}"
                )
        return FetchResult(url=url, final_url=current, redirect_chain=chain, error="too many redirects")


def extract_text(body: bytes, content_type: str, url: str) -> tuple[str | None, str]:
    """Return (title, main text)."""
    raw = body.decode("utf-8", errors="replace")
    if content_type.startswith("text/plain") or content_type == "application/json":
        return None, raw
    try:
        import trafilatura

        text = trafilatura.extract(
            raw, url=url, include_comments=False, include_tables=False, favor_precision=True
        )
        meta = trafilatura.extract_metadata(raw)
        title = meta.title if meta else None
    except Exception:  # noqa: BLE001 - extraction must never crash a run
        text, title = None, None
    if not text:
        import re

        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return title, text
