"""End-to-end tests of MagBot.on_message with fake Stoat objects and a fake LLM.

Real Consent and MemoryStore (":memory:") on tmp_path; nothing touches the network.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import time
from types import SimpleNamespace

import pytest
import stoat

from magbot.bot import (
    ERROR_REPLY,
    MAX_IMAGE_BYTES,
    OPTIN_DONE,
    OPTIN_FAILED,
    OPTOUT_DONE,
    OPTOUT_FAILED,
    REFUSAL,
    MagBot,
    RateLimiter,
)
from magbot.commands import REGISTRY as COMMANDS
from magbot.commands import command
from magbot.config import Config
from magbot.consent import Consent, optin_message
from magbot.identity import User
from magbot.llm import LLMError, Reply
from magbot.memory import MemoryStore

BOT_ID = "01" + "B" * 24
ALICE_ID = "01" + "A" * 24
EVE_ID = "01" + "E" * 24
CAROL_ID = "01" + "C" * 24
ADMIN_ID = "01" + "D" * 24
CHANNEL_ID = "CHANNEL"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeLLM:
    """Records calls and returns a canned Reply (or raises `error`)."""

    def __init__(self):
        self.text = "All good!"
        self.reasoning = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.error: Exception | None = None
        self.calls: list[tuple[list[dict], object]] = []

    async def respond(self, messages, ctx):
        self.calls.append((messages, ctx))
        if self.error is not None:
            raise self.error
        return Reply(text=self.text, reasoning=self.reasoning, provider="fake",
                     prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens)


def person(uid: str, name: str, disc: str = "0002", *, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=uid, name=name, discriminator=disc, display_name=name, bot=bot)


def user_of(uid: str, name: str, disc: str = "0002") -> User:
    return User(uid, f"{name}#{disc}", name)


class FakeAttachment:
    def __init__(self, filename: str, content_type: str, data: bytes = b"", *,
                 size: int | None = None, kind=stoat.AssetMetadataType.image):
        self.id = "ATT" + filename
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self.metadata = SimpleNamespace(type=kind)
        self._data = data
        self.read_calls = 0

    async def read(self) -> bytes:
        self.read_calls += 1
        return self._data


class FakeChannel:
    def __init__(self, *, history=(), channel_type: str = "TextChannel"):
        self.id = CHANNEL_ID
        self.channel_type = channel_type
        self._history = list(history)      # oldest first
        self.by_id: dict[str, FakeMessage] = {}   # messages fetch_message can return
        self.fetch_calls: list[str] = []
        self.fetch_fails = False
        self.history_calls: list[tuple] = []
        self.sent: list[str] = []
        self.typing_starts = 0
        self.typing_fails = False

    async def history(self, *, limit=None, before=None, **_):
        self.history_calls.append((limit, before))
        newest_first = list(reversed(self._history))  # the API returns newest first
        return newest_first[:limit] if limit else newest_first

    async def fetch_message(self, message, **_):
        self.fetch_calls.append(message)
        if self.fetch_fails:
            raise RuntimeError("fetch broke")
        if message not in self.by_id:
            raise LookupError(message)
        return self.by_id[message]

    def typing(self):
        return self._typing()

    @contextlib.asynccontextmanager
    async def _typing(self):
        if self.typing_fails:
            raise RuntimeError("typing unavailable")
        self.typing_starts += 1
        yield

    async def send(self, content=None, **_):
        self.sent.append(content)


class FakeMessage:
    def __init__(self, channel: FakeChannel, author: SimpleNamespace, content: str = "", *,
                 id: str = "MSG", mention_ids=(), attachments=(), replies=()):
        self.id = id
        self.channel = channel
        self.channel_id = channel.id
        self.author = author
        self.author_id = author.id
        self.content = content
        self.mention_ids = list(mention_ids)
        self.attachments = list(attachments)
        self.replies = list(replies)     # ids of the messages this one replies to (as in stoat)
        self.outbox: list[tuple[str, bool]] = []
        self.reactions: list[str] = []
        self.reply_fails = False

    async def reply(self, content=None, *, mention=True, **_):
        if self.reply_fails:
            raise RuntimeError("reply broke")
        self.outbox.append((content, mention))

    async def react(self, emoji, **_):
        self.reactions.append(emoji)


async def say(bot: MagBot, channel: FakeChannel, author: SimpleNamespace, content: str, *,
              pinged: bool = False, attachments=(), id: str = "MSG") -> FakeMessage:
    message = FakeMessage(channel, author, content, id=id,
                          mention_ids=[BOT_ID] if pinged else [], attachments=attachments)
    await bot.on_message(message)
    return message


def reply_texts(message: FakeMessage) -> list[str]:
    return [text for text, _ in message.outbox]


def allow(bot: MagBot, uid: str, name: str, disc: str = "0002") -> None:
    bot.consent.allow(user_of(uid, name, disc))


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config()
    cfg.bot.data_dir = str(tmp_path / "data")
    cfg.bot.www_dir = str(tmp_path / "www")
    cfg.bot.persona_file = str(tmp_path / "missing-persona.md")  # built-in fallback
    cfg.bot.admins = [ADMIN_ID]
    cfg.self_id = BOT_ID
    return cfg


@pytest.fixture
def bot(config):
    memory = MemoryStore(":memory:")
    magbot = MagBot(config, llm=FakeLLM(), consent=Consent(config), memory=memory)
    yield magbot
    memory.close()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def throwaway_command():
    @command("zzcheck", help="test only")
    async def zzcheck(ctx):
        await ctx.reply("pong")

    yield "/zzcheck"
    COMMANDS.pop("/zzcheck", None)


# ── tests ────────────────────────────────────────────────────────────────────

async def test_ignores_own_and_bot_messages(bot, channel):
    await say(bot, channel, person(BOT_ID, "magbot", "0001"), "magbot hi", pinged=True)
    allow(bot, ALICE_ID, "alice")
    await say(bot, channel, person(ALICE_ID, "alice", bot=True), "magbot hi", pinged=True)
    assert bot.llm.calls == []
    assert channel.sent == []


async def test_opt_in_command_allows_user_and_confirms(bot, channel):
    carol = person(CAROL_ID, "carol")
    msg = await say(bot, channel, carol, "magbot opt in")
    assert bot.consent.is_allowed(user_of(CAROL_ID, "carol"))
    assert msg.outbox == [(OPTIN_DONE.format(name="magbot"), True)]
    assert bot.llm.calls == []


async def test_opt_out_excludes_user_and_forgets_their_memories(bot, channel):
    allow(bot, ALICE_ID, "alice")
    bot.memory.add(ALICE_ID, "likes tea")
    bot.memory.add(MemoryStore.GLOBAL, "the server is cozy")
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot opt out")
    alice = user_of(ALICE_ID, "alice")
    assert bot.consent.is_excluded(alice)
    assert not bot.consent.is_allowed(alice)
    assert bot.memory.for_user(ALICE_ID) == []
    assert [m.text for m in bot.memory.for_user(MemoryStore.GLOBAL)] == ["the server is cozy"]
    assert msg.outbox == [(OPTOUT_DONE.format(name="magbot"), True)]


async def test_excluded_user_is_silent_and_never_prompted_again(bot, channel):
    allow(bot, ALICE_ID, "alice")
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot opt out")
    before = len(channel.sent)
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot are you there?", pinged=True)
    assert msg.outbox == [] and msg.reactions == []
    assert len(channel.sent) == before
    assert bot.llm.calls == []


async def test_unopted_user_gets_optin_prompt_once(bot, channel):
    carol = person(CAROL_ID, "carol")
    first = await say(bot, channel, carol, "magbot hi")
    second = await say(bot, channel, carol, "magbot hi again")
    assert first.outbox == [(optin_message(bot.config), False)]
    assert second.outbox == []
    assert bot.llm.calls == []


async def test_unaddressed_message_from_allowed_user_is_ignored(bot, channel):
    allow(bot, ALICE_ID, "alice")
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "nice weather today")
    assert msg.outbox == [] and msg.reactions == []
    assert bot.llm.calls == []


async def test_unaddressed_message_from_unopted_user_gets_no_prompt(bot, channel):
    msg = await say(bot, channel, person(CAROL_ID, "carol"), "nice weather today")
    assert msg.outbox == []
    assert bot.llm.calls == []


async def test_dm_counts_as_addressed(bot):
    allow(bot, ALICE_ID, "alice")
    dm = FakeChannel(channel_type="DirectMessage")
    msg = await say(bot, dm, person(ALICE_ID, "alice"), "hello there")
    assert len(bot.llm.calls) == 1
    assert msg.reactions == ["👀", "✅"]


async def test_command_short_circuits_the_ai(bot, channel, throwaway_command):
    allow(bot, ALICE_ID, "alice")
    msg = await say(bot, channel, person(ALICE_ID, "alice"), throwaway_command)
    assert msg.outbox == [("pong", False)]
    assert bot.llm.calls == []
    assert msg.reactions == []


async def test_unsafe_text_refused_for_non_admin_but_passes_for_admin(bot, channel):
    allow(bot, ALICE_ID, "alice")
    allow(bot, ADMIN_ID, "boss", "0001")
    refused = await say(bot, channel, person(ALICE_ID, "alice"), "magbot rm -rf /", pinged=True)
    assert refused.outbox == [(REFUSAL, True)]
    assert bot.llm.calls == []

    allowed = await say(bot, channel, person(ADMIN_ID, "boss", "0001"), "magbot rm -rf /",
                        pinged=True)
    assert allowed.outbox == [("All good!", False)]
    assert len(bot.llm.calls) == 1


async def test_ping_gets_ai_reply_with_context_from_allowed_authors_only(bot, channel):
    allow(bot, ALICE_ID, "alice")
    bot_author = person(BOT_ID, "magbot", "0001")
    channel._history = [
        FakeMessage(channel, person(ALICE_ID, "alice"), "hello from alice", id="H1"),
        FakeMessage(channel, person(EVE_ID, "eve", "0003"), "secret from eve", id="H2"),
        FakeMessage(channel, bot_author, f"<@{ALICE_ID}> hi there magbot", id="H3"),
    ]
    msg = await say(bot, channel, person(ALICE_ID, "alice"), f"<@{BOT_ID}> what is up",
                    pinged=True)

    assert msg.reactions == ["👀", "✅"]
    assert msg.outbox == [("All good!", False)]
    assert channel.history_calls == [(bot.config.bot.recent_context, "MSG")]

    (messages, ctx), = bot.llm.calls
    user_turn = messages[1]["content"]
    assert "- alice#0002: hello from alice" in user_turn
    assert "- magbot#0001 (bot): hi there" in user_turn
    assert "secret" not in user_turn and "eve" not in user_turn
    assert user_turn.index("hello from alice") < user_turn.index("hi there")
    assert user_turn.endswith("[Message from alice#0002]\nwhat is up")
    assert ctx.user.tag == "alice#0002"
    assert ctx.channel_id == CHANNEL_ID
    assert ctx.memory is bot.memory


async def test_long_reply_is_split_into_chunks(bot, channel):
    allow(bot, ALICE_ID, "alice")
    bot.llm.text = ("lorem ipsum dolor sit amet " * 200).strip()
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot tell me a story",
                    pinged=True)
    assert len(msg.outbox) > 1
    assert all(len(text) <= bot.config.bot.max_message_chars for text, _ in msg.outbox)
    assert all(mention is False for _, mention in msg.outbox)
    joined = " ".join(text for text, _ in msg.outbox).split()
    assert joined == bot.llm.text.split()


@pytest.mark.parametrize("error", [LLMError("all providers down"), RuntimeError("bug")])
async def test_model_failure_sends_error_reply(bot, channel, error):
    allow(bot, ALICE_ID, "alice")
    bot.llm.error = error
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert msg.outbox == [(ERROR_REPLY, False)]


async def test_images_are_passed_to_the_model_and_not_saved(bot, channel):
    allow(bot, ALICE_ID, "alice")
    png = FakeAttachment("cat.png", "image/png", PNG)
    pdf = FakeAttachment("notes.pdf", "application/pdf", b"%PDF",
                         kind=stoat.AssetMetadataType.file)
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot what is this?", pinged=True,
              attachments=[png, pdf])

    (messages, _ctx), = bot.llm.calls
    content = messages[1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    images = [p for p in content if p["type"] == "image_url"]
    assert len(images) == 1
    url = images[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG
    assert pdf.read_calls == 0
    assert list(bot.config.www_path.iterdir()) == []


async def test_image_count_and_size_limits(bot, channel):
    allow(bot, ALICE_ID, "alice")
    pngs = [FakeAttachment(f"{i}.png", "image/png", PNG + bytes([i])) for i in range(5)]
    huge = FakeAttachment("huge.png", "image/png", b"x", size=MAX_IMAGE_BYTES + 1)
    attachments = [pngs[0], pngs[1], huge, pngs[2], pngs[3], pngs[4]]
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot look", pinged=True,
              attachments=attachments)

    (messages, _ctx), = bot.llm.calls
    images = [p for p in messages[1]["content"] if p["type"] == "image_url"]
    assert len(images) == 4
    assert huge.read_calls == 0
    assert pngs[4].read_calls == 0  # the fifth image is over the cap, never read


async def test_reply_failure_falls_back_to_channel_send(bot, channel):
    allow(bot, ALICE_ID, "alice")
    msg = FakeMessage(channel, person(ALICE_ID, "alice"), "magbot hello",
                      mention_ids=[BOT_ID])
    msg.reply_fails = True
    await bot.on_message(msg)
    assert channel.sent == ["All good!"]
    assert msg.reactions == ["👀", "✅"]


@pytest.mark.parametrize(("mode", "traced"), [("off", False), ("blockquote", True)])
async def test_thinking_trace_prepended_when_enabled(bot, channel, mode, traced):
    allow(bot, ALICE_ID, "alice")
    bot.config.bot.thinking_mode = mode
    bot.llm.reasoning = "check the facts first"
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot think", pinged=True)
    (text, _), = msg.outbox
    if traced:
        assert text.startswith("> 💭 **Thinking**")
        assert "> check the facts first" in text
        assert text.endswith("All good!")
    else:
        assert text == "All good!"


async def test_typing_failure_still_produces_a_reply(bot, channel):
    allow(bot, ALICE_ID, "alice")
    channel.typing_fails = True
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert len(bot.llm.calls) == 1
    assert msg.outbox == [("All good!", False)]


async def test_memories_notes_and_overrides_reach_the_prompt(bot, channel, tmp_path):
    allow(bot, ALICE_ID, "alice")
    bot.memory.add(ALICE_ID, "likes tea")
    bot.memory.add(MemoryStore.GLOBAL, "the server is cozy")
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"alice#0002": "always answer in rhymes"}))
    bot.config.bot.overrides_file = str(overrides)
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    (messages, _ctx), = bot.llm.calls
    system = messages[0]["content"]
    assert "likes tea" in system
    assert "the server is cozy" in system
    assert "always answer in rhymes" in system


async def test_opt_out_failure_still_forgets_memories_and_says_so(bot, channel, monkeypatch):
    allow(bot, ALICE_ID, "alice")
    bot.memory.add(ALICE_ID, "likes tea")

    def broken_deny(user):
        raise OSError("disk full")

    monkeypatch.setattr(bot.consent, "deny", broken_deny)
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot opt out")
    assert msg.outbox == [(OPTOUT_FAILED, True)]
    assert bot.memory.for_user(ALICE_ID) == []


async def test_opt_in_failure_is_reported(bot, channel, monkeypatch):
    def broken_allow(user):
        raise OSError("read-only file system")

    monkeypatch.setattr(bot.consent, "allow", broken_allow)
    msg = await say(bot, channel, person(CAROL_ID, "carol"), "magbot opt in")
    assert msg.outbox == [(OPTIN_FAILED, True)]


async def test_on_ready_records_the_bot_id(bot):
    bot.config.self_id = ""
    event = SimpleNamespace(me=SimpleNamespace(id=BOT_ID, tag="magbot#0001"),
                            servers=[SimpleNamespace(name="srv", id="SRV")])
    await bot.on_ready(event)
    assert bot.config.self_id == BOT_ID


# ── rate limit ───────────────────────────────────────────────────────────────

class FakeClock:
    """Stands in for time.monotonic so tests can move the window forward."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(bot) -> FakeClock:
    fake = FakeClock()
    bot.rate_limiter = RateLimiter(clock=fake)
    return fake


async def test_rate_limit_stops_the_model_and_reacts_once(bot, channel, clock):
    allow(bot, ALICE_ID, "alice")
    bot.config.bot.rate_limit_per_minute = 2
    alice = person(ALICE_ID, "alice")
    await say(bot, channel, alice, "magbot one", pinged=True)
    await say(bot, channel, alice, "magbot two", pinged=True)
    third = await say(bot, channel, alice, "magbot three", pinged=True)

    assert third.reactions == ["⏳"]
    assert third.outbox == []
    assert len(bot.llm.calls) == 2
    assert bot.stats["rate_limited"] == 1
    assert bot.stats["replies"] == 2


async def test_rate_limit_window_expires_after_sixty_seconds(bot, channel, clock):
    allow(bot, ALICE_ID, "alice")
    bot.config.bot.rate_limit_per_minute = 1
    alice = person(ALICE_ID, "alice")
    await say(bot, channel, alice, "magbot one", pinged=True)
    clock.now += 59
    blocked = await say(bot, channel, alice, "magbot two", pinged=True)
    assert blocked.reactions == ["⏳"]
    clock.now += 1          # the first request is now exactly 60 seconds old
    allowed = await say(bot, channel, alice, "magbot three", pinged=True)
    assert allowed.reactions == ["👀", "✅"]
    assert len(bot.llm.calls) == 2


async def test_admins_are_exempt_from_the_rate_limit(bot, channel, clock):
    allow(bot, ADMIN_ID, "boss", "0001")
    bot.config.bot.rate_limit_per_minute = 1
    boss = person(ADMIN_ID, "boss", "0001")
    for i in range(3):
        await say(bot, channel, boss, f"magbot {i}", pinged=True)
    assert len(bot.llm.calls) == 3
    assert bot.stats["rate_limited"] == 0


async def test_rate_limit_zero_turns_the_limit_off(bot, channel, clock):
    allow(bot, ALICE_ID, "alice")
    bot.config.bot.rate_limit_per_minute = 0
    for i in range(5):
        await say(bot, channel, person(ALICE_ID, "alice"), f"magbot {i}", pinged=True)
    assert len(bot.llm.calls) == 5


async def test_rate_limit_is_per_user(bot, channel, clock):
    allow(bot, ALICE_ID, "alice")
    allow(bot, EVE_ID, "eve", "0003")
    bot.config.bot.rate_limit_per_minute = 1
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot one", pinged=True)
    blocked = await say(bot, channel, person(ALICE_ID, "alice"), "magbot two", pinged=True)
    other = await say(bot, channel, person(EVE_ID, "eve", "0003"), "magbot hi", pinged=True)
    assert blocked.reactions == ["⏳"]
    assert other.reactions == ["👀", "✅"]


async def test_rate_limited_message_does_not_count_against_the_window(bot, channel, clock):
    allow(bot, ALICE_ID, "alice")
    bot.config.bot.rate_limit_per_minute = 1
    alice = person(ALICE_ID, "alice")
    await say(bot, channel, alice, "magbot one", pinged=True)            # t=1000, allowed
    clock.now += 30
    await say(bot, channel, alice, "magbot two", pinged=True)            # blocked, not recorded
    clock.now += 30                                                      # t=1060
    allowed = await say(bot, channel, alice, "magbot three", pinged=True)
    assert allowed.reactions == ["👀", "✅"]


def test_rate_limiter_drops_idle_keys_and_empty_entries():
    clock = FakeClock()
    limiter = RateLimiter(window=60, clock=clock)
    for uid in ("a", "b", "c"):
        limiter.record(uid)
    assert set(limiter._hits) == {"a", "b", "c"}
    clock.now += 61
    limiter.record("d")                  # a record past the window sweeps the idle keys
    assert set(limiter._hits) == {"d"}
    assert limiter.is_limited("d", 1)
    clock.now += 61
    assert not limiter.is_limited("d", 1)
    assert limiter._hits == {}           # the empty entry is dropped on lookup


def test_rate_limiter_zero_limit_never_limits():
    limiter = RateLimiter(clock=FakeClock())
    limiter.record("a")
    assert not limiter.is_limited("a", 0)


# ── replied-to message ───────────────────────────────────────────────────────

async def test_replied_to_message_from_opted_in_author_reaches_the_prompt(bot, channel):
    allow(bot, ALICE_ID, "alice")
    allow(bot, EVE_ID, "eve", "0003")
    channel.by_id["ORIG"] = FakeMessage(channel, person(EVE_ID, "eve", "0003"),
                                        "the deploy broke at noon", id="ORIG")
    msg = FakeMessage(channel, person(ALICE_ID, "alice"), "magbot why?", id="NEW",
                      mention_ids=[BOT_ID], replies=["ORIG"])
    await bot.on_message(msg)

    assert channel.fetch_calls == ["ORIG"]
    (messages, _ctx), = bot.llm.calls
    assert messages[1]["content"] == (
        "[The message they are replying to]\n"
        "eve#0003: the deploy broke at noon\n\n"
        "[Message from alice#0002]\nwhy?")


async def test_replied_to_message_from_user_without_opt_in_is_skipped(bot, channel):
    allow(bot, ALICE_ID, "alice")
    channel.by_id["ORIG"] = FakeMessage(channel, person(CAROL_ID, "carol"),
                                        "carol's private thing", id="ORIG")
    msg = FakeMessage(channel, person(ALICE_ID, "alice"), "magbot why?", id="NEW",
                      mention_ids=[BOT_ID], replies=["ORIG"])
    await bot.on_message(msg)

    assert channel.fetch_calls == ["ORIG"]
    (messages, _ctx), = bot.llm.calls
    assert "replying to" not in messages[1]["content"]
    assert "private thing" not in messages[1]["content"]
    assert "private thing" not in messages[0]["content"]


async def test_replied_to_fetch_error_still_replies_without_it(bot, channel):
    allow(bot, ALICE_ID, "alice")
    channel.fetch_fails = True
    msg = FakeMessage(channel, person(ALICE_ID, "alice"), "magbot why?", id="NEW",
                      mention_ids=[BOT_ID], replies=["ORIG"])
    await bot.on_message(msg)

    assert len(bot.llm.calls) == 1
    assert msg.outbox == [("All good!", False)]
    (messages, _ctx), = bot.llm.calls
    assert "replying to" not in messages[1]["content"]


async def test_replied_to_message_already_in_context_is_not_fetched(bot, channel):
    allow(bot, ALICE_ID, "alice")
    channel._history = [FakeMessage(channel, person(ALICE_ID, "alice"), "the earlier point",
                                    id="ORIG")]
    msg = FakeMessage(channel, person(ALICE_ID, "alice"), "magbot expand on that", id="NEW",
                      mention_ids=[BOT_ID], replies=["ORIG"])
    await bot.on_message(msg)

    assert channel.fetch_calls == []
    (messages, _ctx), = bot.llm.calls
    user_turn = messages[1]["content"]
    assert "- alice#0002: the earlier point" in user_turn
    assert "replying to" not in user_turn


# ── pending memories ─────────────────────────────────────────────────────────

async def test_pending_memories_are_claimed_before_the_reply(bot, channel, monkeypatch, caplog):
    allow(bot, ALICE_ID, "alice")
    calls: list[tuple[str, str]] = []

    def claim(tag, user_id):
        calls.append((tag, user_id))
        return 2

    monkeypatch.setattr(bot.memory, "claim", claim)
    with caplog.at_level(logging.INFO):
        msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert calls == [("alice#0002", ALICE_ID)]
    assert msg.outbox == [("All good!", False)]
    assert any("claimed 2 pending memories for alice#0002" in r.getMessage()
               for r in caplog.records)


async def test_claim_is_not_called_for_users_without_opt_in(bot, channel, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bot.memory, "claim", lambda tag, uid: calls.append((tag, uid)) or 0)
    await say(bot, channel, person(CAROL_ID, "carol"), "magbot hello", pinged=True)
    assert calls == []


async def test_claim_failure_does_not_block_the_reply(bot, channel, monkeypatch):
    allow(bot, ALICE_ID, "alice")

    def broken(tag, user_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bot.memory, "claim", broken)
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert msg.outbox == [("All good!", False)]


# ── stats ────────────────────────────────────────────────────────────────────

async def test_stats_count_replies_and_tokens(bot, channel):
    allow(bot, ALICE_ID, "alice")
    bot.llm.prompt_tokens = 120
    bot.llm.completion_tokens = 30
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert bot.stats == {"replies": 1, "errors": 0, "rate_limited": 0,
                         "prompt_tokens": 120, "completion_tokens": 30}


async def test_stats_count_model_errors(bot, channel):
    allow(bot, ALICE_ID, "alice")
    bot.llm.error = LLMError("all providers down")
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert bot.stats["errors"] == 1
    assert bot.stats["replies"] == 0


def test_started_at_is_set_at_startup(bot):
    assert abs(bot.started_at - time.time()) < 60


# ── live persona reload ──────────────────────────────────────────────────────

async def test_persona_edits_apply_to_the_next_reply(bot, channel, tmp_path):
    allow(bot, ALICE_ID, "alice")
    persona = tmp_path / "persona.md"
    persona.write_text("VERSION ONE for {bot_name}")
    os.utime(persona, ns=(1_000_000_000_000, 1_000_000_000_000))
    bot.config.bot.persona_file = str(persona)
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)

    persona.write_text("VERSION TWO for {bot_name}")
    os.utime(persona, ns=(2_000_000_000_000, 2_000_000_000_000))
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot again", pinged=True)

    first, second = [messages[0]["content"] for messages, _ in bot.llm.calls]
    assert first.startswith("VERSION ONE for magbot")
    assert second.startswith("VERSION TWO for magbot")


async def test_missing_persona_is_logged_once_not_per_message(bot, channel, caplog):
    allow(bot, ALICE_ID, "alice")
    with caplog.at_level(logging.WARNING):
        for i in range(3):
            await say(bot, channel, person(ALICE_ID, "alice"), f"magbot {i}", pinged=True)
    warnings = [r for r in caplog.records if "persona file" in r.getMessage()]
    assert len(warnings) == 1
    assert len(bot.llm.calls) == 3
