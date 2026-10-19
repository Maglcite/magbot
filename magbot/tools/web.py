"""Web tools: search, and SSRF-safe fetching of pages.

Every request to a URL that came from the model or a chat user goes through
`safe_get`. It only connects to public addresses: the URL host is checked
before each hop, and `SafeResolver` checks again at connect time, so a DNS
answer can't swap in an internal address between check and connect. Redirects
are followed by hand so every hop is re-checked.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from . import ToolContext, ToolError, tool

log = logging.getLogger(__name__)

USER_AGENT = "magbot/2.0"
FETCH_MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_URL_CHARS = 2000
# Only the usual web ports; anything else is refused before any connection.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")
DDG_URL = "https://html.duckduckgo.com/html/"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


class UnsafeTarget(ToolError):
    """The URL points at a non-public address."""


# --- SSRF protection -------------------------------------------------------

def is_public_ip(address: str) -> bool:
    """True only for globally routable addresses.

    IPv4-mapped (::ffff:a.b.c.d) and 6to4 IPv6 addresses are judged by the
    IPv4 address they carry.
    """
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
    return bool(ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified))


async def resolve_public(host: str, port: int, family: int = 0) -> list[tuple[int, str]]:
    """Resolve `host`. Raise unless every address it resolves to is public.

    Returns (family, address) pairs.
    """
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM)
    except OSError:
        raise ToolError(f"could not resolve host {host!r}") from None
    found = []
    for fam, _type, _proto, _canon, sockaddr in infos:
        address = sockaddr[0]
        if not is_public_ip(address):
            raise UnsafeTarget("refusing to fetch a private, local or reserved address")
        found.append((fam, address))
    if not found:
        raise ToolError(f"host {host!r} has no addresses")
    return found


class SafeResolver(AbstractResolver):
    """aiohttp resolver that only hands out public addresses.

    aiohttp sends IP-literal hosts straight to the socket without calling the
    resolver, so `_check_url` also validates literals before any request.
    """

    async def resolve(self, host: str, port: int = 0,
                      family: int = socket.AF_INET) -> list[dict]:
        found = await resolve_public(host, port, family)
        return [{"hostname": host, "host": ip, "port": port, "family": fam,
                 "proto": 0, "flags": 0} for fam, ip in found]

    async def close(self) -> None:
        return None


def parse_url(url: str) -> tuple[urlsplit, int]:
    """Syntax checks for a URL from the model. Returns (parts, port)."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_CHARS:
        raise ToolError("the URL is empty or too long")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError("only http:// and https:// URLs can be fetched")
    if not parts.hostname:
        raise ToolError("the URL has no host name")
    if "@" in parts.netloc:
        raise ToolError("URLs with a user name or password are not allowed")
    try:
        explicit = parts.port
    except ValueError:
        raise ToolError("the URL has an invalid port") from None
    port = explicit or (443 if parts.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise ToolError(f"port {port} is not allowed (use 80, 443, 8080 or 8443)")
    return parts, port


async def _check_url(url: str) -> None:
    parts, port = parse_url(url)
    await resolve_public(parts.hostname, port)


@dataclass
class Fetched:
    url: str            # final URL, after redirects
    status: int
    content_type: str   # lower-case media type, "" when the server sent none
    charset: str | None
    body: bytes
    truncated: bool     # True when the body was cut at the byte limit

    def text(self) -> str:
        try:
            return self.body.decode(self.charset or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


async def safe_get(url: str, *, max_bytes: int, accept: str = "*/*") -> Fetched:
    """GET a public http(s) URL, following at most MAX_REDIRECTS redirects.

    Reads at most `max_bytes` of the body (the rest is dropped, `truncated` is set).
    """
    connector = aiohttp.TCPConnector(resolver=SafeResolver(), use_dns_cache=False)
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    async with aiohttp.ClientSession(connector=connector, timeout=HTTP_TIMEOUT,
                                     trust_env=False) as session:
        for _hop in range(MAX_REDIRECTS + 1):
            await _check_url(url)
            try:
                async with session.get(url, headers=headers, allow_redirects=False) as resp:
                    location = resp.headers.get("Location")
                    if resp.status in REDIRECT_CODES and location:
                        url = urljoin(url, location)
                        continue
                    buf = bytearray()
                    truncated = False
                    async for chunk in resp.content.iter_chunked(65536):
                        buf += chunk
                        if len(buf) > max_bytes:
                            del buf[max_bytes:]
                            truncated = True
                            break
                    media = resp.headers.get("Content-Type", "").split(";", 1)[0]
                    return Fetched(url, resp.status, media.strip().lower(),
                                   resp.charset, bytes(buf), truncated)
            except aiohttp.ClientError as e:
                raise ToolError(f"request failed ({type(e).__name__})") from None
            except asyncio.TimeoutError:
                raise ToolError("the request timed out") from None
        raise ToolError(f"too many redirects (more than {MAX_REDIRECTS})")


# --- HTML to text ----------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
    "article", "header", "footer", "nav", "table", "blockquote", "pre", "ul", "ol",
    "title", "hr",
})


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Readable plain text from HTML: scripts and styles dropped, blocks on their own lines."""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    lines: list[str] = []
    blank = False
    for raw in "".join(parser.parts).splitlines():
        line = " ".join(raw.split())
        if not line:
            if lines and not blank:
                lines.append("")
            blank = True
        else:
            lines.append(line)
            blank = False
    return "\n".join(lines).strip()


@tool(
    "fetch_url",
    "Fetch a web page over http or https and return its readable text (HTML is converted "
    "to plain text). Use it to read a page, e.g. one from a web_search result. Local and "
    "private network addresses are refused.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL starting with http:// or https://."},
        },
        "required": ["url"],
    },
)
async def fetch_url(ctx: ToolContext, url: str) -> str:
    got = await safe_get(url, max_bytes=FETCH_MAX_BYTES,
                         accept="text/html,text/plain,application/json;q=0.9,*/*;q=0.1")
    if not 200 <= got.status < 300:
        raise ToolError(f"the server answered HTTP {got.status}")
    ctype = got.content_type
    if ctype and not ctype.startswith(TEXT_TYPES):
        raise ToolError(f"cannot read {ctype} content as text")
    if not ctype or "html" in ctype:
        text = html_to_text(got.text())
    else:
        text = got.text().strip()
    limit = ctx.config.tools.fetch_max_chars
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n[... cut at {limit} characters]"
    if got.truncated:
        text += f"\n[download stopped at {FETCH_MAX_BYTES // (1024 * 1024)} MB]"
    return f"{got.url}\n\n{text or '(page has no readable text)'}"


# --- Search ----------------------------------------------------------------

@dataclass
class Hit:
    title: str
    url: str
    snippet: str


def _clean(text: object, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def _unwrap_ddg(href: str) -> str:
    """Resolve DuckDuckGo's /l/?uddg=<target> redirect links. "" if not http(s)."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        href = target[0] if target else ""
    return href if urlsplit(href).scheme in ("http", "https") else ""


class _DuckParser(HTMLParser):
    """Collects result links (class result__a) and their snippets (class result__snippet)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, Any]] = []
        self._buf: list[str] | None = None
        self._end_tag = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._buf is not None:
            return
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self.results.append({"href": attr.get("href") or "", "title": [], "snippet": []})
            self._buf, self._end_tag = self.results[-1]["title"], tag
        elif "result__snippet" in classes and self.results:
            self._buf, self._end_tag = self.results[-1]["snippet"], tag

    def handle_endtag(self, tag: str) -> None:
        if self._buf is not None and tag == self._end_tag:
            self._buf = None

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def parse_duckduckgo(html: str) -> list[Hit]:
    """Search hits from a DuckDuckGo HTML results page."""
    parser = _DuckParser()
    parser.feed(html)
    parser.close()
    hits = []
    for r in parser.results:
        url = _unwrap_ddg(r["href"])
        title = _clean("".join(r["title"]))
        if url and title:
            hits.append(Hit(title, url, _clean("".join(r["snippet"]))))
    return hits


async def _request(method: str, url: str, *, as_json: bool = False, **kwargs) -> Any:
    """Plain request to a configured search endpoint (no SSRF filter: the admin chose it)."""
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, trust_env=False,
                                     headers={"User-Agent": USER_AGENT}) as session:
        async with session.request(method, url, **kwargs) as resp:
            if resp.status >= 400:
                raise ToolError(f"search endpoint answered HTTP {resp.status}")
            return await resp.json(content_type=None) if as_json else await resp.text()


async def _duckduckgo(tools_cfg, query: str, limit: int) -> list[Hit]:
    html = await _request("POST", DDG_URL, data={"q": query})
    return parse_duckduckgo(html)[:limit]


async def _searxng(tools_cfg, query: str, limit: int) -> list[Hit]:
    if not tools_cfg.searxng_url:
        raise ToolError("SearXNG URL is not configured")
    base = tools_cfg.searxng_url.rstrip("/")
    data = await _request("GET", f"{base}/search", as_json=True,
                          params={"q": query, "format": "json"})
    return [Hit(_clean(r.get("title")), r.get("url") or "", _clean(r.get("content")))
            for r in data.get("results", [])][:limit]


async def _brave(tools_cfg, query: str, limit: int) -> list[Hit]:
    key = os.environ.get(tools_cfg.brave_api_key_env, "")
    if not key:
        raise ToolError(f"Brave search needs the ${tools_cfg.brave_api_key_env} variable set")
    data = await _request("GET", BRAVE_URL, as_json=True,
                          params={"q": query, "count": limit},
                          headers={"X-Subscription-Token": key, "Accept": "application/json"})
    return [Hit(_clean(r.get("title")), r.get("url") or "", _clean(r.get("description")))
            for r in data.get("web", {}).get("results", [])][:limit]


async def _run_search(tools_cfg, query: str, limit: int) -> list[Hit]:
    backend = tools_cfg.search_backend
    if backend in ("searxng", "brave"):
        try:
            fn = _searxng if backend == "searxng" else _brave
            return await fn(tools_cfg, query, limit)
        except Exception as e:
            log.warning("%s search failed (%s); falling back to DuckDuckGo", backend, e)
    elif backend != "duckduckgo":
        log.warning("unknown search backend %r; using DuckDuckGo", backend)
    return await _duckduckgo(tools_cfg, query, limit)


@tool(
    "web_search",
    "Search the web. Returns the top results as numbered lines with title, URL and a short "
    "snippet. Use it for current events or facts you are unsure of; read a result with fetch_url.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10,
                            "description": "How many results to return (1-10, default 5)."},
        },
        "required": ["query"],
    },
)
async def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    query = _clean(query, 300)
    if not query:
        raise ToolError("the search query is empty")
    limit = min(max(int(max_results), 1), 10)
    try:
        hits = await _run_search(ctx.config.tools, query, limit)
    except Exception as e:
        log.warning("web search failed: %s", type(e).__name__)
        raise ToolError("web search is unavailable right now") from None
    hits = [h for h in hits if urlsplit(h.url).scheme in ("http", "https")]
    if not hits:
        return "No results found."
    lines = []
    for i, h in enumerate(hits[:limit], 1):
        lines.append(f"{i}. {_clean(h.title)}\n   {h.url}")
        if h.snippet:
            lines.append(f"   {_clean(h.snippet)}")
    return "\n".join(lines)
