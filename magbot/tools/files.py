"""File tools for the public web folder (`config.bot.www_dir`).

Names are single plain file names. Every access is checked to stay inside the
folder, and symlinks are refused both by an up-front check and by O_NOFOLLOW
on the open itself, so a link planted between check and open can't redirect a write.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from pathlib import Path

from . import ToolContext, ToolError, tool

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
MAX_WRITE_BYTES = 1_000_000
MAX_READ_BYTES = 2 * 1024 * 1024
LIST_LIMIT = 200
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NAME_HELP = ("a single plain file name such as 'notes.txt': letters, digits, '.', '-' and '_', "
             "starting with a letter or digit, at most 100 characters")


def check_name(name: object) -> str:
    """Return `name` if it is a valid file name, else raise ToolError."""
    # fullmatch, not ^...$: "$" would accept a trailing newline.
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ToolError(f"invalid file name; use {NAME_HELP}")
    return name


def _path(ctx: ToolContext, name: object) -> Path:
    """The path for `name` inside www_dir. Raises ToolError if it would escape."""
    name = check_name(name)
    root = ctx.config.www_path
    path = root / name
    if path.is_symlink():
        raise ToolError("refusing to use a symbolic link in the web folder")
    if path.resolve().parent != root.resolve():
        raise ToolError("file name points outside the web folder")
    return path


def public_url(ctx: ToolContext, name: str) -> str:
    return ctx.config.bot.file_base_url.rstrip("/") + "/" + name


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


async def save(ctx: ToolContext, name: str, data: bytes) -> str:
    """Write `data` as `name` in www_dir. Returns the public URL; records the path in ctx.outputs."""
    path = _path(ctx, name)
    try:
        await asyncio.to_thread(_write, path, data)
    except OSError as e:
        raise ToolError(f"could not write {name!r}: {e.strerror or type(e).__name__}") from None
    ctx.outputs.append(str(path))
    return public_url(ctx, name)


def _read(path: Path, limit: int) -> bytes:
    # O_NONBLOCK so opening a FIFO can't hang; anything but a regular file is refused.
    fd = os.open(path, os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise IsADirectoryError(f"{path.name} is not a regular file")
        return f.read(limit + 1)


def _list(root: Path) -> list[tuple[str, int]]:
    if not root.is_dir():
        return []
    with os.scandir(root) as entries:
        found = [(e.name, e.stat(follow_symlinks=False).st_size)
                 for e in entries if e.is_file(follow_symlinks=False)]
    return sorted(found)


@tool(
    "write_file",
    "Save text as a file in the public web folder and get back its public URL. Use it to share "
    "code, notes or data as a link. The name must be " + NAME_HELP + ". Writing to an existing "
    "name replaces that file.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "File name, e.g. 'notes.txt'."},
            "content": {"type": "string", "description": "The text to write (max 1 MB)."},
        },
        "required": ["name", "content"],
    },
)
async def write_file(ctx: ToolContext, name: str, content: str) -> str:
    data = str(content).encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError("the content is over 1 MB")
    url = await save(ctx, name, data)
    log.info("file %s written by %s (%d bytes)", name, ctx.user.tag, len(data))
    return f"Saved {name} ({len(data)} bytes): {url}"


@tool(
    "read_file",
    "Read a text file from the public web folder. Long files are cut.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "File name, e.g. 'notes.txt'."}},
        "required": ["name"],
    },
)
async def read_file(ctx: ToolContext, name: str) -> str:
    path = _path(ctx, name)
    try:
        data = await asyncio.to_thread(_read, path, MAX_READ_BYTES)
    except FileNotFoundError:
        raise ToolError(f"no file named {name!r}") from None
    except OSError:
        raise ToolError(f"could not read {name!r}") from None
    if b"\0" in data[:8192]:
        raise ToolError(f"{name!r} is a binary file, not text")
    truncated = len(data) > MAX_READ_BYTES
    text = data[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    limit = ctx.config.tools.fetch_max_chars
    if len(text) > limit:
        text = text[:limit] + f"\n[... cut at {limit} characters]"
    elif truncated:
        text += "\n[... file is larger than 2 MB, rest not shown]"
    return text or "(empty file)"


@tool(
    "list_files",
    "List the files in the public web folder with their sizes and public URLs.",
    {"type": "object", "properties": {}},
)
async def list_files(ctx: ToolContext) -> str:
    found = await asyncio.to_thread(_list, ctx.config.www_path)
    if not found:
        return "No files yet."
    lines = [f"{name} ({size} bytes) {public_url(ctx, name)}" for name, size in found[:LIST_LIMIT]]
    if len(found) > LIST_LIMIT:
        lines.append(f"[... {len(found) - LIST_LIMIT} more not shown]")
    return "\n".join(lines)


@tool(
    "delete_file",
    "Delete a file from the public web folder. Admins only.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "File name to delete."}},
        "required": ["name"],
    },
    allowed=lambda ctx: ctx.is_admin,
)
async def delete_file(ctx: ToolContext, name: str) -> str:
    path = _path(ctx, name)
    try:
        await asyncio.to_thread(path.unlink)
    except FileNotFoundError:
        raise ToolError(f"no file named {name!r}") from None
    except OSError:
        raise ToolError(f"could not delete {name!r}") from None
    log.info("file %s deleted by %s", name, ctx.user.tag)
    return f"Deleted {name}."
