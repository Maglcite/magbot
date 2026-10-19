"""Tests for magbot/tools/web.py: SSRF checks, HTML conversion, search parsing, fetch flows.

HTTP flows run against an aiohttp TestServer on 127.0.0.1. The public-address
check is patched to allow loopback for those tests only; the SSRF tests use the
real check.
"""
from __future__ import annotations

import socket

import pytest
from aiohttp import web as aweb
from aiohttp.test_utils import TestServer

from magbot.config import Config
from magbot.identity import User
from magbot.tools import ToolContext, ToolError, call
from magbot.tools import web as mweb


def make_ctx(tmp_path) -> ToolContext:
    cfg = Config()
    cfg.bot.www_dir = str(tmp_path)
    cfg.bot.file_base_url = "https://files.test"
    return ToolContext(config=cfg, user=User("u1", "alice#1001", "Alice"), is_admin=False,
                       can_shell=False, channel_id="chan", memory=None)


DDG_HTML = """
<html><body>
<div class="result results_links results_links_deep web-result">
 <div class="links_main links_deep result__body">
  <h2 class="result__title">
   <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example <b>Page</b> title</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">An example &amp; snippet<b>.</b></a>
 </div>
</div>
<div class="result results_links web-result">
 <h2 class="result__title"><a class="result__a" href="https://direct.example.org/">Direct link</a></h2>
 <div class="result__snippet">Second snippet.</div>
</div>
<div class="result">
 <a class="result__a" href="javascript:alert(1)">Evil</a>
 <a class="result__snippet" href="x">Ignored</a>
</div>
</body></html>
"""


@pytest.fixture(autouse=True)
def no_real_search(monkeypatch):
    """Point the hosted search endpoints at a closed local port so no test reaches the internet."""
    monkeypatch.setattr(mweb, "DDG_URL", "http://127.0.0.1:9/")
    monkeypatch.setattr(mweb, "BRAVE_URL", "http://127.0.0.1:9/")


class _AnyPort:
    def __contains__(self, _port) -> bool:
        return True


@pytest.fixture
def allow_loopback(monkeypatch):
    """Treat 127.0.0.1 as public (only for tests that talk to the TestServer)."""
    monkeypatch.setattr(mweb, "is_public_ip", lambda ip: ip == "127.0.0.1")
    monkeypatch.setattr(mweb, "ALLOWED_PORTS", _AnyPort())


@pytest.fixture
async def site():
    """A local HTTP server with the endpoints the tests need."""
    app = aweb.Application()

    async def html(request):
        return aweb.Response(text="<html><head><style>p{}</style></head><body>"
                                  "<script>var x=1;</script><p>Hello <b>world</b></p>"
                                  "<div>Second line</div></body></html>",
                             content_type="text/html")

    async def hop(request):
        n = int(request.match_info["n"])
        if n == 0:
            return aweb.Response(text="arrived")
        return aweb.Response(status=301, headers={"Location": f"/hop/{n - 1}"})

    async def redirect(request):
        return aweb.Response(status=302, headers={"Location": request.query["to"]})

    async def big(request):
        return aweb.Response(text="a" * (3 * 1024 * 1024), content_type="text/plain")

    async def png(request):
        return aweb.Response(body=b"\x89PNG\r\n\x1a\n", content_type="image/png")

    async def boom(request):
        return aweb.Response(status=500, text="nope")

    async def ddg(request):
        form = await request.post()
        assert form["q"] == "python asyncio"
        return aweb.Response(text=DDG_HTML, content_type="text/html")

    async def searxng(request):
        assert request.query["format"] == "json"
        return aweb.json_response({"results": [
            {"title": "Py docs", "url": "https://docs.example/py", "content": "The docs."},
            {"title": "Bad", "url": "ftp://nope.example/", "content": "x"},
        ]})

    async def searxng_down(request):
        return aweb.Response(status=503)

    async def brave(request):
        assert request.headers["X-Subscription-Token"] == "brave-secret-123"
        return aweb.json_response({"web": {"results": [
            {"title": f"Hit {i}", "url": f"https://b{i}.example/", "description": "d"}
            for i in range(12)]}})

    app.router.add_get("/html", html)
    app.router.add_get("/hop/{n}", hop)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/big", big)
    app.router.add_get("/png", png)
    app.router.add_get("/boom", boom)
    app.router.add_post("/ddg/", ddg)
    app.router.add_get("/searxng/search", searxng)
    app.router.add_get("/searxng-down/search", searxng_down)
    app.router.add_get("/brave", brave)
    async with TestServer(app) as server:
        yield server


# --- SSRF validator ----------------------------------------------------------

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.8.8", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "0.0.0.0", "100.64.0.1", "224.0.0.1", "240.0.0.1", "::1", "::", "fe80::1", "fc00::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "not-an-ip", "",
])
def test_non_public_addresses_are_refused(ip):
    assert mweb.is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111",
                                "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(ip):
    assert mweb.is_public_ip(ip) is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)",
    "http://user:pw@example.com/", "http://user@example.com/", "http:///path",
    "https://example.com:22/", "http://example.com:notaport/", "", "   ",
    "http://example.com/" + "a" * 2000,
])
def test_bad_urls_are_rejected(url):
    with pytest.raises(ToolError):
        mweb.parse_url(url)


def test_good_urls_parse_with_default_ports():
    _, port = mweb.parse_url("https://example.com/a?b=1")
    assert port == 443
    _, port = mweb.parse_url("http://example.com")
    assert port == 80
    _, port = mweb.parse_url("http://example.com:8080/")
    assert port == 8080


async def test_ip_literal_to_private_address_refused_before_connecting(tmp_path):
    ctx = make_ctx(tmp_path)
    for url in ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/"]:
        with pytest.raises(ToolError, match="private"):
            await mweb.fetch_url(ctx, url=url)


async def test_tool_call_reports_ssrf_refusal_as_error_string(tmp_path):
    ctx = make_ctx(tmp_path)
    out = await call("fetch_url", '{"url": "http://127.0.0.1:80/"}', ctx)
    assert out.startswith("error:") and "private" in out


async def test_resolver_returns_public_addresses_in_aiohttp_format():
    rows = await mweb.SafeResolver().resolve("8.8.8.8", 443)
    assert rows == [{"hostname": "8.8.8.8", "host": "8.8.8.8", "port": 443,
                     "family": socket.AF_INET, "proto": 0, "flags": 0}]


async def test_resolver_refuses_private_addresses():
    with pytest.raises(ToolError, match="private"):
        await mweb.SafeResolver().resolve("127.0.0.1", 80)
    with pytest.raises(ToolError, match="private"):
        await mweb.SafeResolver().resolve("169.254.169.254", 80)


# --- HTML and search parsing --------------------------------------------------

def test_html_to_text_drops_scripts_and_styles_and_keeps_blocks():
    text = mweb.html_to_text(
        "<html><head><title>T</title><style>p{color:red}</style></head>"
        "<body><script>evil()</script><p>Hello <b>world</b> &amp; you</p>"
        "<div>Second</div><noscript>no</noscript></body></html>")
    assert "evil" not in text and "color" not in text and "no" not in text.split()
    lines = [line for line in text.splitlines() if line]
    assert lines == ["T", "Hello world & you", "Second"]


def test_duckduckgo_parser_extracts_title_url_and_snippet():
    hits = mweb.parse_duckduckgo(DDG_HTML)
    assert hits == [
        mweb.Hit("Example Page title", "https://example.com/page", "An example & snippet."),
        mweb.Hit("Direct link", "https://direct.example.org/", "Second snippet."),
    ]


@pytest.mark.parametrize("href, expected", [
    ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fx&rut=1", "https://a.example/x"),
    ("https://example.com/direct", "https://example.com/direct"),
    ("javascript:alert(1)", ""),
    ("ftp://files.example/", ""),
])
def test_duckduckgo_redirect_links_are_unwrapped(href, expected):
    assert mweb._unwrap_ddg(href) == expected


# --- fetch_url over a local TestServer ----------------------------------------

async def test_fetch_html_is_converted_to_text(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    out = await mweb.fetch_url(ctx, url=str(site.make_url("/html")))
    assert out.startswith(str(site.make_url("/html")))
    assert "Hello world" in out and "Second line" in out
    assert "var x" not in out


async def test_fetch_follows_five_redirects_but_not_six(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    assert "arrived" in await mweb.fetch_url(ctx, url=str(site.make_url("/hop/5")))
    with pytest.raises(ToolError, match="too many redirects"):
        await mweb.fetch_url(ctx, url=str(site.make_url("/hop/6")))


async def test_redirect_to_private_address_is_refused(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    url = str(site.make_url("/redirect")) + "?to=http://169.254.169.254/latest/"
    with pytest.raises(ToolError, match="private"):
        await mweb.fetch_url(ctx, url=url)


async def test_redirect_to_url_with_credentials_is_refused(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    url = str(site.make_url("/redirect")) + "?to=http://u:p@127.0.0.1/"
    with pytest.raises(ToolError, match="user name or password"):
        await mweb.fetch_url(ctx, url=url)


async def test_redirect_to_non_http_scheme_is_refused(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    url = str(site.make_url("/redirect")) + "?to=file:///etc/passwd"
    with pytest.raises(ToolError, match="only http"):
        await mweb.fetch_url(ctx, url=url)


async def test_hostname_resolving_to_loopback_is_refused_without_patch(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="private|resolve"):
        await mweb.fetch_url(ctx, url="http://localhost:80/")


async def test_fetch_of_hostname_goes_through_safe_resolver(site, monkeypatch, tmp_path):
    # Hostnames are resolved by SafeResolver at connect time; allow both loopback
    # forms that "localhost" may resolve to.
    monkeypatch.setattr(mweb, "is_public_ip", lambda ip: ip in ("127.0.0.1", "::1"))
    monkeypatch.setattr(mweb, "ALLOWED_PORTS", _AnyPort())
    ctx = make_ctx(tmp_path)
    url = f"http://localhost:{site.port}/html"
    assert "Hello world" in await mweb.fetch_url(ctx, url=url)


async def test_binary_content_is_refused(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="image/png"):
        await mweb.fetch_url(ctx, url=str(site.make_url("/png")))


async def test_http_error_status_is_reported(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="HTTP 500"):
        await mweb.fetch_url(ctx, url=str(site.make_url("/boom")))


async def test_download_is_capped_and_text_is_cut(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.tools.fetch_max_chars = 100
    out = await mweb.fetch_url(ctx, url=str(site.make_url("/big")))
    assert "download stopped at 2 MB" in out
    assert "cut at 100 characters" in out
    assert len(out) < 400


# --- web_search ----------------------------------------------------------------

async def test_web_search_duckduckgo_formats_numbered_results(site, allow_loopback,
                                                             monkeypatch, tmp_path):
    monkeypatch.setattr(mweb, "DDG_URL", str(site.make_url("/ddg/")))
    ctx = make_ctx(tmp_path)
    out = await mweb.web_search(ctx, query="python asyncio")
    assert out == ("1. Example Page title\n   https://example.com/page\n"
                   "   An example & snippet.\n"
                   "2. Direct link\n   https://direct.example.org/\n   Second snippet.")


async def test_web_search_searxng_backend(site, allow_loopback, tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.tools.search_backend = "searxng"
    ctx.config.tools.searxng_url = str(site.make_url("/searxng"))
    out = await mweb.web_search(ctx, query="python")
    assert out == "1. Py docs\n   https://docs.example/py\n   The docs."


async def test_web_search_falls_back_to_duckduckgo_when_searxng_fails(site, allow_loopback,
                                                                     monkeypatch, tmp_path):
    monkeypatch.setattr(mweb, "DDG_URL", str(site.make_url("/ddg/")))
    ctx = make_ctx(tmp_path)
    ctx.config.tools.search_backend = "searxng"
    ctx.config.tools.searxng_url = str(site.make_url("/searxng-down"))
    out = await mweb.web_search(ctx, query="python asyncio")
    assert "Example Page title" in out


async def test_web_search_brave_sends_key_header_and_never_echoes_it(site, allow_loopback,
                                                                    monkeypatch, tmp_path):
    monkeypatch.setattr(mweb, "BRAVE_URL", str(site.make_url("/brave")))
    monkeypatch.setenv("TEST_BRAVE_KEY", "brave-secret-123")
    ctx = make_ctx(tmp_path)
    ctx.config.tools.search_backend = "brave"
    ctx.config.tools.brave_api_key_env = "TEST_BRAVE_KEY"
    out = await mweb.web_search(ctx, query="anything", max_results=3)
    assert "1. Hit 0" in out and "3. Hit 2" in out and "4." not in out
    assert "brave-secret-123" not in out


async def test_web_search_brave_without_key_falls_back(site, allow_loopback, monkeypatch,
                                                      tmp_path):
    monkeypatch.setattr(mweb, "DDG_URL", str(site.make_url("/ddg/")))
    monkeypatch.delenv("TEST_BRAVE_KEY_MISSING", raising=False)
    ctx = make_ctx(tmp_path)
    ctx.config.tools.search_backend = "brave"
    ctx.config.tools.brave_api_key_env = "TEST_BRAVE_KEY_MISSING"
    assert "Direct link" in await mweb.web_search(ctx, query="python asyncio")


async def test_web_search_reports_unavailable_when_everything_fails(site, allow_loopback,
                                                                   monkeypatch, tmp_path):
    monkeypatch.setattr(mweb, "DDG_URL", str(site.make_url("/boom")))
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="unavailable"):
        await mweb.web_search(ctx, query="python")


async def test_web_search_rejects_empty_query_and_clamps_results(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="empty"):
        await mweb.web_search(ctx, query="   ")

    async def fake(tools_cfg, query, limit):
        return [mweb.Hit(f"T{i}", f"https://x{i}.example/", "") for i in range(50)][:limit]

    monkeypatch.setattr(mweb, "_run_search", fake)
    out = await mweb.web_search(ctx, query="q", max_results=99)
    assert out.count("https://") == 10


def test_fetched_text_uses_declared_charset():
    got = mweb.Fetched("u", 200, "text/plain", "latin-1", "é".encode("latin-1"), False)
    assert got.text() == "é"
    bad = mweb.Fetched("u", 200, "text/plain", "no-such-charset", b"ok", False)
    assert bad.text() == "ok"
