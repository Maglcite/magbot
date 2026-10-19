"""MagBot: the Stoat client and the on_message pipeline.

This module is glue only. Each step is delegated to its own module (consent,
identity, commands, safety, memory, prompt, llm, formatting); the order of the
steps in `_on_message` is the legacy `on_message` order and must stay that way.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import re
from typing import Any, AsyncIterator

import stoat

from . import commands, formatting, identity, prompt, safety
from .config import Config
from .consent import Consent, optin_message, parse_command
from .identity import User
from .llm import LLM, LLMError, Reply
from .memory import MemoryStore
from .tools import ToolContext

log = logging.getLogger(__name__)

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
CONTEXT_CHARS = 400          # per recent-context line
GLOBAL = MemoryStore.GLOBAL

REFUSAL = (
    "🚫 I can't help with that — it looks like it could be destructive "
    "or malicious. I'm happy to help with code, games, questions, "
    "broadcasting, and other harmless stuff though! >w<"
)
ERROR_REPLY = "⚠️ Sorry, I hit an error and couldn't process that. Try again in a moment!"
OPTIN_DONE = (
    "yay, thank you! ✨ i've added you to my opt-in list — i'll read and reply to your "
    "messages from now on. say `{name} opt out` any time if you change your mind 💛"
)
OPTIN_ALREADY = (
    "you're already opted in — i'm reading & replying to you. ✨ (`{name} opt out` to undo)"
)
OPTIN_FAILED = "Hmm, I couldn't update the opt-in list. Please contact the operator. ⚠️"
OPTIN_BLOCKED = "Sorry, the operator has excluded your account, so I can't opt you in. 🔒"
OPTOUT_DONE = (
    "Done — I've removed you from my opt-in list and won't read or reply to your messages. "
    "Say `{name} opt in` if you change your mind. 🔒"
)
OPTOUT_ALREADY = "You're already opted out of my AI context — your messages aren't used. 👍"
OPTOUT_FAILED = "Hmm, I couldn't update the opt-out list. Please contact the operator. ⚠️"

_DM_CHANNELS = (stoat.DMChannel, stoat.GroupChannel, stoat.SavedMessagesChannel)
_DM_TYPES = {"DirectMessage", "Group", "SavedMessages"}


def _is_dm_like(channel: Any) -> bool:
    """DM, group and saved-messages channels: everything there is addressed to the bot."""
    if isinstance(channel, _DM_CHANNELS):
        return True
    kind = getattr(channel, "channel_type", None)
    return str(getattr(kind, "value", kind) or "") in _DM_TYPES


def _names_in(text: str, names: list[str]) -> bool:
    """True if any configured bot name appears in `text` as a whole word (any case)."""
    return any(
        re.search(r"(?<!\w)" + re.escape(n.strip()) + r"(?!\w)", text, re.IGNORECASE)
        for n in names if n.strip()
    )


def _attachments(message: Any) -> list[Any]:
    return list(getattr(message, "attachments", None) or [])


def _image_mime(att: Any) -> str | None:
    """The MIME type if this attachment is an image, else None."""
    ctype = (getattr(att, "content_type", "") or "").lower()
    meta_type = getattr(getattr(att, "metadata", None), "type", None)
    meta_kind = str(getattr(meta_type, "value", meta_type) or "").lower()
    if not (ctype.startswith("image/") or meta_kind == "image"):
        return None
    if ctype.startswith("image/"):
        return ctype
    guessed = mimetypes.guess_type(getattr(att, "filename", "") or "")[0]
    return guessed if guessed and guessed.startswith("image/") else "image/png"


@contextlib.asynccontextmanager
async def _typing(channel: Any) -> AsyncIterator[None]:
    """Show the typing indicator around a block. If it cannot start or stop, the block
    still runs: a missing indicator must never block the reply."""
    cm = None
    try:
        cm = channel.typing()
        await cm.__aenter__()
    except Exception as e:
        log.warning("typing indicator unavailable: %s", e)
        cm = None
    try:
        yield
    finally:
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception as e:
                log.warning("typing indicator failed to stop: %s", e)


class MagBot(stoat.Client):
    """The Stoat client. Answers pings, name mentions and DMs from opted-in users."""

    def __init__(self, config: Config, *, llm: LLM | None = None,
                 consent: Consent | None = None, memory: MemoryStore | None = None,
                 **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config
        self.llm = llm if llm is not None else LLM(config)
        self.consent = consent if consent is not None else Consent(config)
        self.memory = memory if memory is not None else MemoryStore(config.data_path / "memory.db")
        self.persona = prompt.load_persona(config)
        self.overrides = prompt.load_overrides(config)
        self._slots = asyncio.Semaphore(config.bot.max_concurrent)
        config.www_path.mkdir(parents=True, exist_ok=True)
        config.data_path.mkdir(parents=True, exist_ok=True)

    # ── events ───────────────────────────────────────────────────────────

    async def on_ready(self, event, /) -> None:
        me = event.me
        self.config.self_id = str(me.id)
        log.info("Logged in as %s (ID: %s)", me.tag, me.id)
        for server in event.servers or []:
            log.info("  Server: %s (ID: %s)", server.name, server.id)
        try:
            ids, tags, excluded = self.consent.counts()
            log.info("Consent: opt-in list active: %d account ids / %d tag aliases allowed; "
                     "%d excluded", ids, tags, excluded)
        except Exception as e:
            log.warning("Consent: could not summarise the lists: %s", e)

    async def on_message(self, message, /) -> None:
        try:
            await self._on_message(message)
        except Exception:
            log.exception("message handling failed")

    # ── pipeline ─────────────────────────────────────────────────────────

    async def _on_message(self, message) -> None:
        # 1. Ignore our own messages and other bots.
        if self._is_own_or_bot(message):
            return

        # 2. Who sent it, and is it addressed to us?
        user = identity.from_message(message)
        text = message.content or ""
        me_id = self._me_id()
        pinged = bool(me_id) and me_id in (message.mention_ids or [])
        named = _names_in(text, self.config.bot.names)
        is_dm = _is_dm_like(message.channel)

        # 3. Consent commands ("magbot opt in" / "magbot opt out").
        if await self._handle_consent(message, user, text, pinged=pinged, is_dm=is_dm):
            return

        # 4. Opted-out users are dropped silently.
        if self.consent.is_excluded(user):
            log.info("Dropped message from excluded user %s", user.tag)
            return

        # 5. Not opted in: send the opt-in prompt if they addressed us, then stop.
        if not self.consent.is_allowed(user):
            await self._prompt_optin(message, user, addressed=pinged or named)
            return

        # 6. Nothing to answer.
        if not text.strip() and not _attachments(message):
            return

        is_admin = identity.is_admin(user, self.config)

        # 7. Chat commands run whether or not the bot was addressed.
        if await self._handle_command(message, user, is_admin):
            return

        # 8. Only pings, name mentions and DMs get an AI reply.
        if not (pinged or named or is_dm):
            return

        # 9. Dangerous requests from non-admins are refused.
        if not is_admin and not safety.is_safe(text):
            log.warning("Blocked dangerous message from %s: %s", user.tag, text[:100])
            await self._send(message, REFUSAL, mention=True)
            return

        # 10. The AI path, bounded by the concurrency limit.
        async with self._slots:
            await self._reply_with_ai(message, user, text, is_admin)

    def _is_own_or_bot(self, message) -> bool:
        me_id = self._me_id()
        if me_id and str(message.author_id) == me_id:
            return True
        try:
            author = message.author
        except Exception:
            return False
        return bool(getattr(author, "bot", False))

    def _me_id(self) -> str:
        """The bot's own user id: from the live session, else from config (set on ready)."""
        me = self.me
        if me is not None:
            return str(me.id)
        return self.config.self_id

    # ── step helpers ─────────────────────────────────────────────────────

    async def _handle_consent(self, message, user: User, text: str, *,
                              pinged: bool, is_dm: bool) -> bool:
        """Answer an opt-in or opt-out command. True when one was handled."""
        command = parse_command(text, self.config.bot.names, pinged=pinged, is_dm=is_dm)
        if command is None:
            return False
        if command == "in":
            reply = self._opt_in(user)
        else:
            reply = self._opt_out(user)
        await self._send(message, reply, mention=True)
        return True

    def _opt_in(self, user: User) -> str:
        name = self._name()
        try:
            added = self.consent.allow(user)
        except OSError as e:
            log.error("could not save opt-in for %s: %s", user.tag, e)
            return OPTIN_FAILED
        if self.consent.is_excluded(user):   # excluded in config.toml: opting in can't lift it
            log.info("opt-in from operator-excluded %s (%s)", user.tag, user.id)
            return OPTIN_BLOCKED
        if added:
            log.info("%s (%s) opted in", user.tag, user.id)
            return OPTIN_DONE.format(name=name)
        log.info("opt-in from already-allowed %s (%s)", user.tag, user.id)
        return OPTIN_ALREADY.format(name=name)

    def _opt_out(self, user: User) -> str:
        name = self._name()
        try:
            newly = self.consent.deny(user)
        except OSError as e:
            log.error("could not save opt-out for %s: %s", user.tag, e)
            newly = None
        # Forget their memories whatever happened to the list.
        try:
            forgotten = self.memory.forget_user(user.id)
            log.info("opt-out by %s: forgot %d memories", user.tag, forgotten)
        except Exception:
            log.exception("could not forget memories for %s", user.tag)
        if newly is None:
            return OPTOUT_FAILED
        if newly:
            log.info("%s (%s) opted out", user.tag, user.id)
            return OPTOUT_DONE.format(name=name)
        log.info("opt-out from already-excluded %s (%s)", user.tag, user.id)
        return OPTOUT_ALREADY

    async def _prompt_optin(self, message, user: User, *, addressed: bool) -> None:
        """Send the opt-in prompt to someone who addressed us but has not opted in."""
        if not addressed:
            log.info("Blocked %s: not opted in", user.tag)
            return
        if not self.consent.should_prompt(user):
            log.info("Opt-in prompt for %s suppressed (cooldown)", user.tag)
            return
        if await self._send(message, optin_message(self.config), mention=False):
            self.consent.mark_prompted(user)
            log.info("Sent opt-in prompt to %s", user.tag)

    async def _handle_command(self, message, user: User, is_admin: bool) -> bool:
        try:
            return await commands.dispatch(self, message, self.config, user, is_admin)
        except Exception:
            log.exception("command dispatch failed for %s", user.tag)
            return True  # never let a broken command fall through to the AI

    async def _reply_with_ai(self, message, user: User, text: str, is_admin: bool) -> None:
        await self._react(message, "👀")
        query = formatting.strip_address(text, self.config.bot.names) or "Hello!"
        can_shell = identity.can_shell(user, self.config)
        log.info("AI request from %s (admin=%s) in channel %s: %s",
                 user.tag, is_admin, message.channel_id, query[:100])

        context = await self._recent_context(message)
        images = await self._images(message)
        messages = prompt.build_messages(
            persona=self.persona,
            user=user,
            is_admin=is_admin,
            can_shell=can_shell,
            text=query,
            context=context,
            memories=self._memories(user.id),
            notes=self._memories(GLOBAL),
            override=self._override(user),
            images=images,
        )
        ctx = ToolContext(
            config=self.config,
            user=user,
            is_admin=is_admin,
            can_shell=can_shell,
            channel_id=str(message.channel_id),
            memory=self.memory,
        )

        reply = await self._generate(message, messages, ctx)
        if reply is None:
            await self._send(message, ERROR_REPLY)
        else:
            await self._send_reply(message, reply)
        await self._react(message, "✅")

    async def _generate(self, message, messages: list[dict], ctx: ToolContext) -> Reply | None:
        """Run the model with the typing indicator on. None if the reply failed."""
        try:
            async with _typing(message.channel):
                return await self.llm.respond(messages, ctx)
        except LLMError as e:
            log.error("LLM reply failed for %s: %s", ctx.user.tag, e)
        except Exception:
            log.exception("unexpected error generating a reply for %s", ctx.user.tag)
        return None

    async def _send_reply(self, message, reply: Reply) -> None:
        """Format the model's reply (markdown fix, thinking trace, splitting) and send it."""
        text = formatting.sanitize_markdown(reply.text)
        thinking = formatting.format_thinking(reply.reasoning, self.config)
        if thinking:
            log.info("thinking trace: %d chars of reasoning", len(reply.reasoning))
            text = f"{thinking}\n\n{text}"
        for chunk in formatting.split_message(text, self.config.bot.max_message_chars):
            await self._send(message, chunk, mention=False)

    # ── context gathering ────────────────────────────────────────────────

    async def _recent_context(self, message) -> list[prompt.ContextLine]:
        """The messages just before this one, oldest first, from opted-in authors only."""
        limit = self.config.bot.recent_context
        if limit <= 0:
            return []
        try:
            history = await message.channel.history(limit=limit, before=message.id)
        except Exception as e:
            log.warning("could not read recent context: %s", e)
            return []
        me_id = self._me_id()
        names = self.config.bot.names
        lines: list[prompt.ContextLine] = []
        skipped = 0
        for old in reversed(list(history or [])):  # the API returns newest first
            try:
                author = identity.from_message(old)
                if not self.consent.is_allowed(author):
                    skipped += 1
                    continue
                body = formatting.strip_address(old.content or "", names)[:CONTEXT_CHARS]
                if not body:
                    continue
                lines.append(prompt.ContextLine(
                    author=author.tag, text=body, is_bot=bool(me_id) and author.id == me_id))
            except Exception as e:
                log.debug("skipped a context message: %s", e)
        if skipped:
            log.info("context: omitted %d message(s) from authors without opt-in", skipped)
        return lines

    async def _images(self, message) -> list[prompt.Image]:
        """Image attachments as bytes, at most MAX_IMAGES, each <= MAX_IMAGE_BYTES. Not saved."""
        images: list[prompt.Image] = []
        for att in _attachments(message):
            mime = _image_mime(att)
            if mime is None:
                continue
            if len(images) >= MAX_IMAGES:
                log.info("more than %d images attached; the rest are ignored", MAX_IMAGES)
                break
            if (getattr(att, "size", 0) or 0) > MAX_IMAGE_BYTES:
                log.info("skipping oversized image %s", getattr(att, "filename", ""))
                continue
            try:
                data = await att.read()
            except Exception as e:
                log.warning("could not read attachment %s: %s", getattr(att, "filename", ""), e)
                continue
            if not data or len(data) > MAX_IMAGE_BYTES:
                continue
            images.append(prompt.Image(data=data, mime=mime,
                                       name=getattr(att, "filename", "") or "image"))
        return images

    def _memories(self, user_id: str) -> list:
        try:
            return self.memory.for_user(user_id)
        except Exception:
            log.exception("could not read memories for %s", user_id)
            return []

    def _override(self, user: User) -> str | None:
        return self.overrides.get(user.tag) or self.overrides.get(user.id)

    def _name(self) -> str:
        return (self.config.bot.names or ["magbot"])[0]

    # ── Stoat calls (each one can fail without killing the handler) ──────

    async def _send(self, message, text: str, *, mention: bool = False) -> bool:
        """Reply to `message`, falling back to a plain channel send. True if it went out."""
        try:
            await message.reply(text, mention=mention)
            return True
        except Exception as e:
            log.warning("reply failed (%s); falling back to channel.send", e)
        try:
            await message.channel.send(text)
            return True
        except Exception as e:
            log.error("fallback send failed: %s", e)
            return False

    async def _react(self, message, emoji: str) -> None:
        try:
            await message.react(emoji)
        except Exception as e:
            log.warning("could not react %s: %s", emoji, e)
