"""Shell tool: run a command in the web folder. Only for users with `can_shell`."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess

from .. import safety
from ..config import Config
from . import ToolContext, ToolError, tool

log = logging.getLogger(__name__)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASS", "AUTH", "CREDENTIAL", "COOKIE")
MAX_COMMAND_CHARS = 2000


def _child_env(config: Config) -> dict[str, str]:
    """The bot's environment without API keys and tokens, so `env` can't reveal them."""
    named = {p.api_key_env for p in config.llm.providers}
    named |= {config.tools.brave_api_key_env, config.tools.image_api_key_env, "STOAT_BOT_TOKEN"}
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in named or any(word in upper for word in SECRET_WORDS):
            continue
        env[key] = value
    return env


async def _drain(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read the stream to EOF, keeping the first `limit` bytes. Memory stays bounded."""
    buf = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if len(buf) < limit:
            buf += chunk[:limit - len(buf)]
    return bytes(buf), total > limit


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command's whole process group (it was started as its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _collect(proc: asyncio.subprocess.Process, limit: int) -> tuple[bytes, bool]:
    data, truncated = await _drain(proc.stdout, limit)
    await proc.wait()
    return data, truncated


@tool(
    "run_shell",
    "Run a shell command (sh -c) on the bot's machine, with the public web folder as the working "
    "directory. Returns the exit code and the combined output, cut to a length limit. The command "
    "is killed if it runs too long.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command line to run."},
        },
        "required": ["command"],
    },
    allowed=lambda ctx: ctx.can_shell,
)
async def run_shell(ctx: ToolContext, command: str) -> str:
    command = str(command)
    if not command.strip():
        raise ToolError("the command is empty")
    if len(command) > MAX_COMMAND_CHARS:
        raise ToolError(f"the command is over {MAX_COMMAND_CHARS} characters")
    if not ctx.is_admin and not safety.is_safe(command):
        log.warning("shell command refused by safety filter for %s", ctx.user.tag)
        raise ToolError("that command was refused by the safety filter")

    cfg = ctx.config.tools
    cwd = ctx.config.www_path
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_shell(
        command,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=_child_env(ctx.config),
        start_new_session=True,
    )
    try:
        data, truncated = await asyncio.wait_for(_collect(proc, cfg.shell_max_output * 4),
                                                 cfg.shell_timeout)
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        raise ToolError(f"the command timed out after {cfg.shell_timeout:g} s and was killed") from None
    except asyncio.CancelledError:
        _kill_group(proc)
        raise

    # Cut the bytes first, then decode, strip escape codes and cap the characters.
    text = ANSI_RE.sub("", data.decode("utf-8", errors="replace")).replace("\x1b", "")
    if truncated or len(text) > cfg.shell_max_output:
        text = text[:cfg.shell_max_output].rstrip() + "\n[... output cut]"
    return f"exit code {proc.returncode}\n{text.rstrip() or '(no output)'}"
