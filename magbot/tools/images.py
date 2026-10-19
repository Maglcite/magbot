"""Image generation through an OpenAI-compatible /images/generations endpoint."""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import uuid

import aiohttp

from . import ToolContext, ToolError, files, tool
from .web import USER_AGENT, safe_get

log = logging.getLogger(__name__)

SIZES = ("256x256", "512x512", "1024x1024", "1024x1792", "1792x1024")
MAX_PROMPT_CHARS = 1000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=180, connect=10)


def _image_ext(data: bytes) -> str | None:
    """File extension for PNG, JPEG or WebP data (by magic bytes), else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def _request_image(tools_cfg, payload: dict) -> dict:
    """POST to the image API. The API key is sent as a header and never returned or logged."""
    url = tools_cfg.image_base_url.rstrip("/") + "/images/generations"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key = os.environ.get(tools_cfg.image_api_key_env, "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with aiohttp.ClientSession(timeout=IMAGE_TIMEOUT, trust_env=False) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    raise ToolError(f"the image service answered HTTP {resp.status}")
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise ToolError("the image service could not be reached") from None
    except ValueError:
        raise ToolError("the image service sent invalid JSON") from None


async def _image_bytes(item: object) -> bytes:
    """The image from one API result item: inline base64, or a URL fetched SSRF-safely."""
    if not isinstance(item, dict):
        raise ToolError("the image service returned no image")
    if item.get("b64_json"):
        try:
            raw = base64.b64decode(item["b64_json"], validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise ToolError("the image service returned bad image data") from None
    elif item.get("url"):
        got = await safe_get(str(item["url"]), max_bytes=MAX_IMAGE_BYTES, accept="image/*")
        if not 200 <= got.status < 300:
            raise ToolError(f"downloading the image failed (HTTP {got.status})")
        if got.truncated:
            raise ToolError("the generated image is too large")
        raw = got.body
    else:
        raise ToolError("the image service returned no image")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ToolError("the generated image is too large")
    return raw


@tool(
    "generate_image",
    "Generate an image from a text description. The image is saved to the public web folder; "
    "the reply contains its URL, which you should share with the user.",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What the image should show."},
            "size": {"type": "string", "enum": list(SIZES), "default": "1024x1024",
                     "description": "Image size in pixels."},
        },
        "required": ["prompt"],
    },
    allowed=lambda ctx: bool(ctx.config.tools.image_base_url),
)
async def generate_image(ctx: ToolContext, prompt: str, size: str = "1024x1024") -> str:
    prompt = str(prompt).strip()
    if not prompt:
        raise ToolError("the image prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ToolError(f"the image prompt is over {MAX_PROMPT_CHARS} characters")
    if size not in SIZES:
        raise ToolError(f"size must be one of: {', '.join(SIZES)}")

    tools_cfg = ctx.config.tools
    payload = {
        "model": tools_cfg.image_model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
    }
    body = await _request_image(tools_cfg, payload)
    items = body.get("data") if isinstance(body, dict) else None
    raw = await _image_bytes(items[0] if items else None)

    ext = _image_ext(raw)
    if ext is None:
        raise ToolError("the image service did not return a PNG, JPEG or WebP image")
    name = f"image-{uuid.uuid4().hex[:12]}.{ext}"
    url = await files.save(ctx, name, raw)
    log.info("image %s generated for %s (%s)", name, ctx.user.tag, size)
    return f"Generated the image and saved it: {url}"
