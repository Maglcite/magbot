"""Chat commands (`/ban`, `/del-all`, ...) handled before the AI sees a message.

Adding a command: create a module in this package and decorate an async
function with @command. It receives a CommandContext and replies through
ctx.reply(). Then import the module at the bottom of this file.

    @command("ping", help="Check the bot is alive.")
    async def ping(ctx: CommandContext) -> None:
        await ctx.reply("pong")

Commands are written `/name args`. `aliases` adds other spellings; aliases
that don't start with "/" are plain words (Stoat's slash menu sometimes
swallows slash commands, so e.g. `banbanbanban` stands in for `/ban`).
A leading @-ping or bot name is ignored, so "magbot /ban @x" works too.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import Config
from ..identity import User

log = logging.getLogger(__name__)


@dataclass
class CommandContext:
    client: object          # the running stoat.Client (magbot.bot.MagBot)
    message: object         # the stoat.Message that triggered the command
    config: Config
    user: User
    is_admin: bool
    name: str               # the spelling that was used, e.g. "/purge"
    args: str               # text after the command name, stripped

    async def reply(self, text: str) -> None:
        try:
            await self.message.reply(text, mention=False)
        except Exception as e:
            log.warning("command reply failed: %s", e)


@dataclass
class Command:
    name: str
    func: Callable[[CommandContext], Awaitable[None]]
    aliases: tuple[str, ...] = ()
    admin: bool = False
    help: str = ""

    @property
    def spellings(self) -> tuple[str, ...]:
        return ("/" + self.name,) + self.aliases


REGISTRY: dict[str, Command] = {}


def command(name: str, *, aliases: tuple[str, ...] | list[str] = (), admin: bool = False,
            help: str = ""):
    def register(func):
        cmd = Command(name, func, tuple(aliases), admin, help)
        for s in cmd.spellings:
            if s.lower() in REGISTRY:
                raise ValueError(f"command spelling {s!r} registered twice")
            REGISTRY[s.lower()] = cmd
        return func
    return register


def _strip_address(text: str, names: list[str]) -> str:
    """Drop leading @-pings and bot names: "magbot /ban @x" -> "/ban @x"."""
    alts = [r"<@[0-9A-Za-z]+>"] + [re.escape(n) for n in sorted(names, key=len, reverse=True)]
    return re.sub(r"^(?:\s*(?:" + "|".join(alts) + r")[\s,:]*)+", "", text.strip(), flags=re.I)


def parse(text: str, names: list[str]) -> tuple[Command, str, str] | None:
    """Return (command, spelling_used, args) if `text` starts with a command."""
    body = _strip_address(text, names)
    low = body.lower()
    for spelling in sorted(REGISTRY, key=len, reverse=True):
        if not low.startswith(spelling):
            continue
        nxt = body[len(spelling):len(spelling) + 1]
        if nxt and (nxt.isalnum() or nxt in "-_"):
            continue    # "/banana" is not "/ban"
        return REGISTRY[spelling], body[:len(spelling)], body[len(spelling):].strip()
    return None


async def dispatch(client, message, config: Config, user: User, is_admin: bool) -> bool:
    """Run the command in `message`, if any. Returns True when one was handled."""
    parsed = parse(message.content or "", config.bot.names)
    if parsed is None:
        return False
    cmd, spelling, args = parsed
    ctx = CommandContext(client, message, config, user, is_admin, spelling, args)
    if cmd.admin and not is_admin:
        log.warning("non-admin %s tried %s", user.tag, spelling)
        await ctx.reply("🚫 Only admins can use that command.")
        return True
    log.info("command %s by %s: %s", spelling, user.tag, args[:80])
    try:
        await cmd.func(ctx)
    except Exception as e:
        log.exception("command %s failed", spelling)
        await ctx.reply(f"⚠️ {spelling} failed: {str(e)[:200]}")
    return True


# Built-in commands register themselves on import.
from . import help, info, moderation, purge  # noqa: E402,F401
