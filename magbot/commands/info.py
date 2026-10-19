"""/status (admins) and /forget-me (everyone): bot health and self-service memory deletion."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import CommandContext, command

log = logging.getLogger(__name__)

_UNKNOWN = "?"
# MemoryStore.for_user() caps its result; this is high enough to count every global note.
_GLOBAL_LIMIT = 10_000
_FORGET_FAILED = "⚠️ i couldn't clear your memories just now. try again in a bit."


def format_uptime(seconds: float) -> str:
    """'9s', '2m', '3h 12m', '2d 4h'."""
    total = int(max(0.0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _providers_text(config) -> str:
    providers = list(getattr(getattr(config, "llm", None), "providers", None) or [])
    if not providers:
        return "none configured"
    # Names and models only: base URLs and key variable names stay out of chat.
    return " → ".join(f"`{getattr(p, 'name', '?')}: {getattr(p, 'model', '?')}`" for p in providers)


def _search_text(config) -> str:
    tools = getattr(config, "tools", None)
    if tools is None:
        return _UNKNOWN
    if "web_search" in (tools.disabled or []):
        return "off"
    return tools.search_backend


def _memory_text(store) -> str:
    """'<total> stored, <global> of them global notes, <pending> pending'."""
    if store is None:
        return _UNKNOWN
    return f"{_total_memories(store)} stored, {_global_notes(store)} of them global notes, " \
           f"{_pending(store)} pending"


def _total_memories(store) -> str:
    # Read-only count through the store's sqlite handle; the store's lock guards it.
    db = getattr(store, "_db", None)
    if db is None:
        return _UNKNOWN
    lock = getattr(store, "_lock", None)
    guard = lock if lock is not None else contextlib.nullcontext()
    try:
        with guard:
            (count,) = db.execute("SELECT COUNT(*) FROM memories").fetchone()
        return f"{count:,}"
    except Exception as e:
        log.debug("status: could not count memories: %s", e)
        return _UNKNOWN


def _global_notes(store) -> str:
    try:
        return str(len(store.for_user(getattr(store, "GLOBAL", "*"), limit=_GLOBAL_LIMIT)))
    except Exception as e:
        log.debug("status: could not read global notes: %s", e)
        return _UNKNOWN


def _pending(store) -> str:
    counter = getattr(store, "pending_count", None)
    if counter is None:
        return _UNKNOWN
    try:
        return str(int(counter()))
    except Exception as e:
        log.debug("status: could not read pending count: %s", e)
        return _UNKNOWN


def _consent_text(consent) -> str:
    if consent is None:
        return _UNKNOWN
    try:
        ids, tags, excluded = consent.counts()
    except Exception as e:
        log.debug("status: could not read consent counts: %s", e)
        return _UNKNOWN
    # An opted-in user is usually listed twice (by id and by tag), so the two are not summed.
    return f"{ids} by id, {tags} by tag opted in; {excluded} excluded"


def _count(stats: dict, key: str) -> str:
    try:
        return f"{int(stats.get(key, 0) or 0):,}"
    except (TypeError, ValueError):
        return _UNKNOWN


@command("status", admin=True,
         help="Show bot health: uptime, model, usage and data counts.")
async def status(ctx: CommandContext) -> None:
    client = ctx.client
    config = getattr(client, "config", None) or ctx.config
    started = getattr(client, "started_at", None)
    uptime = format_uptime(time.time() - started) if started else _UNKNOWN
    stats = getattr(client, "stats", None)
    if not isinstance(stats, dict):
        stats = {}
    lines = [
        "**magbot status**",
        f"• **uptime:** {uptime}",
        f"• **providers:** {_providers_text(config)}",
        f"• **search:** {_search_text(config)}",
        f"• **since start:** {_count(stats, 'replies')} replies, {_count(stats, 'errors')} errors, "
        f"{_count(stats, 'rate_limited')} rate-limited",
        f"• **tokens since start:** {_count(stats, 'prompt_tokens')} in, "
        f"{_count(stats, 'completion_tokens')} out",
        f"• **memories:** {_memory_text(getattr(client, 'memory', None))}",
        f"• **consent:** {_consent_text(getattr(client, 'consent', None))}",
    ]
    await ctx.reply("\n".join(lines))


@command("forget-me", aliases=("forgetme",),
         help="Delete everything the bot has remembered about you (you stay opted in).")
async def forget_me(ctx: CommandContext) -> None:
    store = getattr(ctx.client, "memory", None)
    if store is None:
        await ctx.reply(_FORGET_FAILED)
        return
    try:
        count = await asyncio.to_thread(store.forget_user, ctx.user.id, ctx.user.tag)
    except Exception:
        log.exception("/forget-me failed for %s", ctx.user.tag)
        await ctx.reply(_FORGET_FAILED)
        return
    if count:
        noun = "thing" if count == 1 else "things"
        await ctx.reply(f"done~ i forgot {count} {noun} about you 🫧")
    else:
        await ctx.reply("i didn't have anything saved about you ✨")
