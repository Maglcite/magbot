"""Tests for magbot/tools/images.py against a fake /images/generations endpoint."""
from __future__ import annotations

import base64

import pytest
from aiohttp import web as aweb
from aiohttp.test_utils import TestServer

from magbot.config import Config
from magbot.identity import User
from magbot.tools import ToolContext, ToolError, call
from magbot.tools import images as mimages
from magbot.tools import web as mweb

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
KEY = "image-secret-key-789"


def make_ctx(tmp_path, base_url: str) -> ToolContext:
    cfg = Config()
    cfg.bot.www_dir = str(tmp_path)
    cfg.bot.file_base_url = "https://files.test"
    cfg.tools.image_base_url = base_url
    cfg.tools.image_model = "test-image-model"
    cfg.tools.image_api_key_env = "MAGBOT_TEST_IMAGE_KEY"
    return ToolContext(config=cfg, user=User("u1", "alice#1001", "Alice"), is_admin=False,
                       can_shell=False, channel_id="chan", memory=None)


class _AnyPort:
    def __contains__(self, _port) -> bool:
        return True


@pytest.fixture
def allow_loopback(monkeypatch):
    monkeypatch.setattr(mweb, "is_public_ip", lambda ip: ip == "127.0.0.1")
    monkeypatch.setattr(mweb, "ALLOWED_PORTS", _AnyPort())


@pytest.fixture
async def api(monkeypatch):
    """Fake image API. The path prefix (b64, url, ...) picks the response mode."""
    monkeypatch.setenv("MAGBOT_TEST_IMAGE_KEY", KEY)
    seen: list[dict] = []

    async def generations(request):
        mode = request.match_info["mode"]
        seen.append({"body": await request.json(), "auth": request.headers.get("Authorization")})
        if mode == "err":
            return aweb.Response(status=500, text=f"internal error {KEY}")
        if mode == "b64":
            return aweb.json_response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        if mode == "url":
            return aweb.json_response({"data": [{"url": str(request.url.with_path("/files/img.png"))}]})
        if mode == "htmlurl":
            return aweb.json_response({"data": [{"url": str(request.url.with_path("/files/page.html"))}]})
        if mode == "badb64":
            return aweb.json_response({"data": [{"b64_json": "%%% not base64 %%%"}]})
        if mode == "notpng":
            return aweb.json_response({"data": [{"b64_json": base64.b64encode(b"<html>").decode()}]})
        if mode == "privurl":
            return aweb.json_response({"data": [{"url": "http://169.254.169.254/x.png"}]})
        return aweb.json_response({"data": []})

    async def png(request):
        return aweb.Response(body=PNG, content_type="image/png")

    async def page(request):
        return aweb.Response(text="<html>not an image</html>", content_type="text/html")

    app = aweb.Application()
    app.router.add_post("/{mode}/images/generations", generations)
    app.router.add_get("/files/img.png", png)
    app.router.add_get("/files/page.html", page)
    async with TestServer(app) as server:
        yield server, seen


async def test_b64_response_is_saved_as_png_and_url_returned(api, tmp_path):
    server, seen = api
    ctx = make_ctx(tmp_path, str(server.make_url("/b64")))
    out = await mimages.generate_image(ctx, prompt="a red fox", size="512x512")

    saved = list(tmp_path.glob("image-*.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == PNG
    assert out.startswith("Generated the image and saved it: https://files.test/image-")
    assert ctx.outputs == [str(saved[0])]

    body = seen[0]["body"]
    assert body == {"model": "test-image-model", "prompt": "a red fox", "size": "512x512",
                    "n": 1, "response_format": "b64_json"}
    assert seen[0]["auth"] == f"Bearer {KEY}"


async def test_api_key_never_appears_in_results(api, tmp_path):
    server, _ = api
    ctx = make_ctx(tmp_path, str(server.make_url("/err")))
    with pytest.raises(ToolError) as info:
        await mimages.generate_image(ctx, prompt="x")
    assert KEY not in str(info.value)
    assert "HTTP 500" in str(info.value)

    out = await call("generate_image", '{"prompt": "x"}', ctx)
    assert KEY not in out and out.startswith("error:")


async def test_url_response_is_downloaded_through_the_safe_fetcher(api, tmp_path, allow_loopback):
    server, _ = api
    ctx = make_ctx(tmp_path, str(server.make_url("/url")))
    await mimages.generate_image(ctx, prompt="a lake")
    assert [p.read_bytes() for p in tmp_path.glob("image-*.png")] == [PNG]


async def test_url_response_pointing_at_private_address_is_refused(api, tmp_path, monkeypatch):
    server, _ = api
    # Only the port allowlist is relaxed (the TestServer port is random); the real
    # address check stays on and must refuse loopback.
    monkeypatch.setattr(mweb, "ALLOWED_PORTS", _AnyPort())
    ctx = make_ctx(tmp_path, str(server.make_url("/url")))
    with pytest.raises(ToolError, match="private"):
        await mimages.generate_image(ctx, prompt="a lake")
    assert not list(tmp_path.glob("image-*"))


async def test_private_url_in_api_response_is_refused(api, tmp_path):
    server, _ = api
    ctx = make_ctx(tmp_path, str(server.make_url("/privurl")))
    with pytest.raises(ToolError, match="private"):
        await mimages.generate_image(ctx, prompt="a lake")


async def test_non_image_url_content_is_not_saved(api, tmp_path, allow_loopback):
    server, _ = api
    ctx = make_ctx(tmp_path, str(server.make_url("/htmlurl")))
    with pytest.raises(ToolError, match="did not return a PNG, JPEG or WebP"):
        await mimages.generate_image(ctx, prompt="a lake")
    assert not list(tmp_path.glob("image-*"))


async def test_bad_base64_and_empty_results_are_errors(api, tmp_path):
    server, _ = api
    ctx = make_ctx(tmp_path, str(server.make_url("/badb64")))
    with pytest.raises(ToolError, match="bad image data"):
        await mimages.generate_image(ctx, prompt="a lake")

    ctx = make_ctx(tmp_path, str(server.make_url("/empty")))
    with pytest.raises(ToolError, match="no image"):
        await mimages.generate_image(ctx, prompt="a lake")

    ctx = make_ctx(tmp_path, str(server.make_url("/notpng")))
    with pytest.raises(ToolError, match="did not return"):
        await mimages.generate_image(ctx, prompt="a lake")


async def test_size_must_be_on_the_allowlist(api, tmp_path):
    server, seen = api
    ctx = make_ctx(tmp_path, str(server.make_url("/b64")))
    with pytest.raises(ToolError, match="size must be one of"):
        await mimages.generate_image(ctx, prompt="a lake", size="2048x2048")
    assert seen == []  # nothing sent to the API


@pytest.mark.parametrize("size", mimages.SIZES)
async def test_every_allowed_size_is_accepted(api, tmp_path, size):
    server, seen = api
    ctx = make_ctx(tmp_path, str(server.make_url("/b64")))
    await mimages.generate_image(ctx, prompt="a lake", size=size)
    assert seen[-1]["body"]["size"] == size


async def test_empty_and_overlong_prompts_are_refused(api, tmp_path):
    server, seen = api
    ctx = make_ctx(tmp_path, str(server.make_url("/b64")))
    with pytest.raises(ToolError, match="empty"):
        await mimages.generate_image(ctx, prompt="   ")
    with pytest.raises(ToolError, match="over"):
        await mimages.generate_image(ctx, prompt="x" * (mimages.MAX_PROMPT_CHARS + 1))
    assert seen == []


async def test_tool_is_hidden_when_no_image_endpoint_is_configured(tmp_path):
    ctx = make_ctx(tmp_path, "")
    out = await call("generate_image", '{"prompt": "a lake"}', ctx)
    assert out == "error: tool 'generate_image' is not available"


def test_image_magic_bytes():
    assert mimages._image_ext(PNG) == "png"
    assert mimages._image_ext(b"\xff\xd8\xff\xe0jpeg") == "jpg"
    assert mimages._image_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert mimages._image_ext(b"<html>") is None
