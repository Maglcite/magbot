"""/del-all: delete one user's messages in a channel (legacy purge feature).

Always a dry run first: `/del-all @user` only counts and remembers the batch;
`/del-all @user confirm` within PURGE_CONFIRM_TTL seconds deletes it. Stoat
rules shape the delete: the bulk endpoint takes at most 100 ids per request
and refuses messages a week old or more, so older ones go one at a time. Both
paths need Manage Messages in the channel.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import stoat

from . import CommandContext, command
from .moderation import server_of, target_user_id

log = logging.getLogger(__name__)

PURGE_SCAN_LIMIT = 2000                    # messages of history scanned per preview
PURGE_CONFIRM_TTL = 300                    # seconds a preview waits for a confirm
PURGE_BULK_MAX = 100                       # Stoat's cap on ids per bulk-delete request
PURGE_BULK_MAX_AGE = 7 * 24 * 60 * 60      # the bulk endpoint rejects anything older
PURGE_PACE = 0.35                          # seconds between delete calls
PURGE_MAX_DELETE = 500                     # per-confirm ceiling
# (channel_id, target_id) -> {"by": author_id, "expires": unix_time, "ids": [message ids]}
PURGE_PENDING: dict[tuple[str, str], dict] = {}

_CONFIRM_WORDS = ("confirm", "yes", "delete", "do it", "go", "y")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_USAGE = ("⚠️ Usage: `/del-all @user` (dry run), then `/del-all @user confirm` to delete.\n"
          "Aliases: `/delall`, `/del_all`, `/purge` — or the word forms `delalldelalldelall` / "
          "`purgepurgepurge` if Stoat swallows the slash command.")
_NOT_IN_SERVER = "⚠️ Use purge in a server text channel — I need its history to scan."


@dataclass
class _Tally:
    deleted: int = 0
    gone: int = 0
    failed: int = 0
    fatal: str | None = None
    first_err: str | None = None


@command("del-all", aliases=("/delall", "/del_all", "/purge", "delalldelalldelall", "purgepurgepurge"),
         admin=True,
         help="Delete one user's messages in this channel. `/del-all @user` is a dry run; "
              "`/del-all @user confirm` deletes them.")
async def del_all(ctx: CommandContext) -> None:
    confirm = _is_confirm(ctx.args)
    target_id = target_user_id(ctx.args)
    channel = getattr(ctx.message, "channel", None)
    server = server_of(ctx.message) if channel is not None else None
    if channel is None or server is None or not hasattr(channel, "history"):
        await ctx.reply(_NOT_IN_SERVER)
        return
    channel_id = str(channel.id)
    now = time.time()

    for key in [k for k, v in PURGE_PENDING.items() if v.get("expires", 0) < now]:
        PURGE_PENDING.pop(key, None)

    if confirm and not target_id:
        keys = [k for k in PURGE_PENDING if k[0] == channel_id]
        if len(keys) != 1:
            hint = (f"{len(keys)} previews pending — mention the user." if keys else
                    "No pending preview for this channel — run `/del-all @user` first.")
            await ctx.reply(f"⌛ {hint}")
            return
        target_id = keys[0][1]

    if not target_id:
        await ctx.reply(_USAGE)
        return

    err = await _permission_error(ctx.client, channel, server)
    if err:
        await ctx.reply(err)
        return

    key = (channel_id, target_id)
    if not confirm:
        await _preview(ctx, channel, channel_id, target_id, now)
        return

    entry = PURGE_PENDING.pop(key, None)
    if entry is None or entry.get("expires", 0) < now:
        await ctx.reply(f"⌛ No fresh preview for that user in this channel "
                        f"(previews last {PURGE_CONFIRM_TTL // 60} min). "
                        f"Run `/del-all @user` first.")
        return

    ids = entry["ids"]
    label = await _target_label(ctx.client, target_id)
    try:
        await ctx.message.react("🧹")
    except Exception:
        pass
    log.info("purge confirmed by %s in %s: deleting %d message(s) from %s",
             ctx.user.tag, channel_id, len(ids), target_id)
    tally, left = await _delete(ctx.client.http, channel, ids)

    bits = [f"🧹 Purged **{tally.deleted}** message(s) from {label} in <#{channel_id}>."]
    if tally.gone:
        bits.append(f" {tally.gone} were already gone.")
    if tally.failed:
        bits.append(f" {tally.failed} failed — first error: {str(tally.first_err)[:160]}")
    if tally.fatal:
        bits.append(f" ⚠️ Stopped early: {tally.fatal[:160]} — grant **Manage Messages** and retry.")
    if left:
        bits.append(f" {left} not attempted — one confirm handles {PURGE_MAX_DELETE}; "
                    f"run the preview again to continue.")
    await ctx.reply("".join(bits))
    log.info("purge result in %s: deleted=%d gone=%d failed=%d left=%d fatal=%s",
             channel_id, tally.deleted, tally.gone, tally.failed, left, bool(tally.fatal))


async def _preview(ctx: CommandContext, channel, channel_id: str, target_id: str, now: float) -> None:
    """Dry run: count the target's messages and remember them for a confirm."""
    try:
        ids, scanned = await _scan(channel, target_id)
    except Exception as e:
        log.error("purge scan failed in %s: %s", channel_id, e)
        await ctx.reply(f"⚠️ Scan failed: {str(e)[:200]}")
        return
    label = await _target_label(ctx.client, target_id)
    if not ids:
        await ctx.reply(f"🔎 Scanned the last {scanned} message(s) in "
                        f"<#{channel_id}> — no messages from {label} found.")
        return
    cutoff = time.time() - PURGE_BULK_MAX_AGE + 120
    recent = sum(1 for i in ids if _ulid_time(i) >= cutoff)
    PURGE_PENDING[(channel_id, target_id)] = {"by": ctx.user.id, "expires": now + PURGE_CONFIRM_TTL,
                                              "ids": ids}
    extra = (f"\n• One confirm deletes at most {PURGE_MAX_DELETE} — repeat for the rest."
             if len(ids) > PURGE_MAX_DELETE else "")
    await ctx.reply(
        f"🧹 Dry run in <#{channel_id}> — scanned the latest {scanned} message(s): "
        f"found **{len(ids)}** from {label}.\n"
        f"• {recent} younger than 1 week → bulk delete\n"
        f"• {len(ids) - recent} older → deleted one by one\n"
        f"Reply `/del-all @user confirm` within {PURGE_CONFIRM_TTL // 60} min to delete them — "
        f"nothing has been deleted yet.{extra}")
    log.info("purge preview by %s in %s: %d/%d from %s",
             ctx.user.tag, channel_id, len(ids), scanned, target_id)


def _is_confirm(args: str) -> bool:
    low = args.lower()
    return any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in _CONFIRM_WORDS)


def _ulid_time(mid) -> float:
    """Seconds since the epoch encoded in a ULID-style id's first 10 chars (0.0 if unparsable)."""
    s = str(mid)
    if len(s) < 10:
        return 0.0
    n = 0
    for ch in s[:10].upper():
        i = _CROCKFORD.find(ch)
        if i < 0:
            return 0.0
        n = (n << 5) | i
    return n / 1000.0


def _error_kind(err: Exception) -> str:
    """'fatal' (stop everything), 'gone' (already deleted) or 'failed'."""
    blob = (str(getattr(err, "type", "") or "") + " " + str(err or "")).lower()
    if any(k in blob for k in ("missingpermission", "missing permission", "forbidden",
                               "unauthorized", "notauthorised", "not authorised",
                               "notauthenticated")):
        return "fatal"
    if any(k in blob for k in ("unknownmessage", "unknown message", "notfound", "not found")):
        return "gone"
    return "failed"


async def _permission_error(client, channel, server) -> str | None:
    """None if the bot may delete other people's messages here, else a warning."""
    me_member = server.get_member(client.me.id)
    if me_member is None:
        try:
            me_member = await server.fetch_member(client.me.id)
        except Exception as e:
            return f"⚠️ Couldn't verify my permissions: {str(e)[:160]}"
    try:
        perms = channel.permissions_for(me_member)
    except Exception:
        try:
            perms = channel.permissions_for(me_member, safe=False)
        except Exception as e:
            return f"⚠️ Couldn't verify my permissions: {str(e)[:160]}"
    if not perms.manage_messages:
        return ("⚠️ I need the **Manage Messages** permission in this channel to delete "
                "other people's messages — grant it to my role and retry.")
    return None


async def _scan(channel, target_id: str) -> tuple[list[str], int]:
    """Walk history newest-first; return (ids authored by target, messages scanned)."""
    ids: list[str] = []
    scanned = 0
    before = None
    while scanned < PURGE_SCAN_LIMIT:
        page = min(100, PURGE_SCAN_LIMIT - scanned)
        batch = await channel.history(limit=page, before=before, sort=stoat.MessageSort.latest,
                                      populate_users=False)
        if not batch:
            break
        for m in batch:
            scanned += 1
            if str(getattr(m, "author_id", "") or "") == target_id:
                ids.append(str(m.id))
        if len(batch) < page:
            break
        before = str(batch[-1].id)
    return ids, scanned


async def _target_label(client, target_id: str) -> str:
    """A readable name for replies. Never a live mention, so no pings."""
    try:
        user = await client.fetch_user(target_id)
        name = getattr(user, "name", None)
        disc = getattr(user, "discriminator", None)
        if name:
            return f"{name}#{disc}" if disc else str(name)
    except Exception:
        pass
    return f"`{target_id}`"


async def _delete(http, channel, ids: list[str]) -> tuple[_Tally, int]:
    """Delete ids newest-first: bulk for recent ones, singles for old ones.

    Returns the tally and how many ids were left unattempted.
    """
    tally = _Tally()
    budget = PURGE_MAX_DELETE
    cutoff = time.time() - PURGE_BULK_MAX_AGE + 120
    recent = [i for i in ids if _ulid_time(i) >= cutoff]
    old = [i for i in ids if _ulid_time(i) < cutoff]

    async def delete_one(mid: str) -> None:
        nonlocal budget
        try:
            await http.delete_message(channel, mid)
            tally.deleted += 1
            budget -= 1
        except Exception as e:
            kind = _error_kind(e)
            if kind == "gone":
                tally.gone += 1
            elif kind == "fatal":
                tally.fatal = str(e)
            else:
                tally.failed += 1
                if tally.first_err is None:
                    tally.first_err = str(e)
        await asyncio.sleep(PURGE_PACE)

    i = 0
    while i < len(recent) and budget > 0 and not tally.fatal:
        chunk = recent[i:i + min(PURGE_BULK_MAX, budget)]
        i += len(chunk)
        try:
            await http.delete_messages(channel, chunk)
            tally.deleted += len(chunk)
            budget -= len(chunk)
        except Exception as e:
            if _error_kind(e) == "fatal":
                tally.fatal = str(e)
                break
            # Bulk refused (e.g. an age race): retry the chunk one by one.
            for mid in chunk:
                if budget <= 0 or tally.fatal:
                    break
                await delete_one(mid)
        if not tally.fatal:
            await asyncio.sleep(PURGE_PACE)

    for mid in old:
        if budget <= 0 or tally.fatal:
            break
        await delete_one(mid)

    left = len(ids) - tally.deleted - tally.gone - tally.failed
    return tally, left
