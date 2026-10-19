"""Admin moderation: /ban, /kick and /unban (legacy `_handle_moderation`)."""
from __future__ import annotations

import logging
import re

from . import CommandContext, command

log = logging.getLogger(__name__)

_MENTION = re.compile(r"<@([0-9A-Za-z]+)>")
# A bare Stoat user id: "01" followed by 24 Crockford base32 characters.
_BARE_ID = re.compile(r"\b(01[0-9A-HJKMNP-TV-Z]{24})\b")

_USAGE = ("⚠️ Usage: `banbanbanban @user [reason]` (or `/ban @user [reason]`) — "
          "mention the user or paste their ID.")
_NO_SERVER = "⚠️ Can't determine the server from this message. Use this in a server channel."


def target_user_id(text: str) -> str | None:
    """The first <@ID> mention, else the first bare Stoat user id, in `text`."""
    m = _MENTION.search(text) or _BARE_ID.search(text)
    return m.group(1) if m else None


def reason_text(text: str) -> str | None:
    """`text` without mentions and bare ids, stripped; None if nothing is left."""
    stripped = _MENTION.sub("", text)
    stripped = _BARE_ID.sub("", stripped)
    return stripped.strip() or None


def server_of(message):
    """The server a message was sent in, or None (DMs, or a server-less channel)."""
    try:
        server = message.server
    except Exception:
        server = None
    if server is None:
        channel = getattr(message, "channel", None)
        try:
            server = getattr(channel, "server", None) if channel is not None else None
        except Exception:
            server = None
    return server


async def _moderate(ctx: CommandContext, action: str) -> None:
    target = target_user_id(ctx.args)
    if not target:
        await ctx.reply(_USAGE)
        return
    reason = reason_text(ctx.args)
    server = server_of(ctx.message)
    if server is None:
        await ctx.reply(_NO_SERVER)
        return

    log.info("admin %s executing /%s on %s (reason: %s)", ctx.user.tag, action, target, reason)
    try:
        if action == "ban":
            await server.ban(target, reason=reason)
            text = f"🔨 Banned <@{target}>{f' — reason: {reason}' if reason else ''}."
        elif action == "kick":
            member = await server.fetch_member(target)
            if member:
                await member.kick()
            else:
                await server.kick(target)
            text = f"👢 Kicked <@{target}>."
        else:
            await server.unban(target)
            text = f"✅ Unbanned <@{target}>."
    except Exception as e:
        err = str(e)
        log.error("/%s failed for %s: %s", action, target, err)
        await ctx.reply(f"⚠️ /{action} failed: {err[:200]}")
        return
    log.info("/%s succeeded for %s by %s", action, target, ctx.user.tag)
    await ctx.reply(text)


@command("ban", aliases=("banbanbanban",), admin=True,
         help="Ban a user from this server. Usage: `/ban @user [reason]`.")
async def ban(ctx: CommandContext) -> None:
    await _moderate(ctx, "ban")


@command("kick", admin=True,
         help="Kick a user from this server. Usage: `/kick @user`.")
async def kick(ctx: CommandContext) -> None:
    await _moderate(ctx, "kick")


@command("unban", admin=True,
         help="Lift a server ban. Usage: `/unban @user` (or paste their user ID).")
async def unban(ctx: CommandContext) -> None:
    await _moderate(ctx, "unban")
