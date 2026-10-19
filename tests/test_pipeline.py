"""End-to-end tests of MagBot.on_message with fake Stoat objects and a fake LLM.

Real Consent and MemoryStore (":memory:") on tmp_path; nothing touches the network.
"""
from __future__ import annotations

import base64
import contextlib
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
        self.error: Exception | None = None
        self.calls: list[tuple[list[dict], object]] = []

    async def respond(self, messages, ctx):
        self.calls.append((messages, ctx))
        if self.error is not None:
            raise self.error
        return Reply(text=self.text, reasoning=self.reasoning, provider="fake")


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
        self.history_calls: list[tuple] = []
        self.sent: list[str] = []
        self.typing_starts = 0
        self.typing_fails = False

    async def history(self, *, limit=None, before=None, **_):
        self.history_calls.append((limit, before))
        newest_first = list(reversed(self._history))  # the API returns newest first
        return newest_first[:limit] if limit else newest_first

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
                 id: str = "MSG", mention_ids=(), attachments=()):
        self.id = id
        self.channel = channel
        self.channel_id = channel.id
        self.author = author
        self.author_id = author.id
        self.content = content
        self.mention_ids = list(mention_ids)
        self.attachments = list(attachments)
        self.replies: list[tuple[str, bool]] = []
        self.reactions: list[str] = []
        self.reply_fails = False

    async def reply(self, content=None, *, mention=True, **_):
        if self.reply_fails:
            raise RuntimeError("reply broke")
        self.replies.append((content, mention))

    async def react(self, emoji, **_):
        self.reactions.append(emoji)


async def say(bot: MagBot, channel: FakeChannel, author: SimpleNamespace, content: str, *,
              pinged: bool = False, attachments=(), id: str = "MSG") -> FakeMessage:
    message = FakeMessage(channel, author, content, id=id,
                          mention_ids=[BOT_ID] if pinged else [], attachments=attachments)
    await bot.on_message(message)
    return message


def reply_texts(message: FakeMessage) -> list[str]:
    return [text for text, _ in message.replies]


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
    assert msg.replies == [(OPTIN_DONE.format(name="magbot"), True)]
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
    assert msg.replies == [(OPTOUT_DONE.format(name="magbot"), True)]


async def test_excluded_user_is_silent_and_never_prompted_again(bot, channel):
    allow(bot, ALICE_ID, "alice")
    await say(bot, channel, person(ALICE_ID, "alice"), "magbot opt out")
    before = len(channel.sent)
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot are you there?", pinged=True)
    assert msg.replies == [] and msg.reactions == []
    assert len(channel.sent) == before
    assert bot.llm.calls == []


async def test_unopted_user_gets_optin_prompt_once(bot, channel):
    carol = person(CAROL_ID, "carol")
    first = await say(bot, channel, carol, "magbot hi")
    second = await say(bot, channel, carol, "magbot hi again")
    assert first.replies == [(optin_message(bot.config), False)]
    assert second.replies == []
    assert bot.llm.calls == []


async def test_unaddressed_message_from_allowed_user_is_ignored(bot, channel):
    allow(bot, ALICE_ID, "alice")
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "nice weather today")
    assert msg.replies == [] and msg.reactions == []
    assert bot.llm.calls == []


async def test_unaddressed_message_from_unopted_user_gets_no_prompt(bot, channel):
    msg = await say(bot, channel, person(CAROL_ID, "carol"), "nice weather today")
    assert msg.replies == []
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
    assert msg.replies == [("pong", False)]
    assert bot.llm.calls == []
    assert msg.reactions == []


async def test_unsafe_text_refused_for_non_admin_but_passes_for_admin(bot, channel):
    allow(bot, ALICE_ID, "alice")
    allow(bot, ADMIN_ID, "boss", "0001")
    refused = await say(bot, channel, person(ALICE_ID, "alice"), "magbot rm -rf /", pinged=True)
    assert refused.replies == [(REFUSAL, True)]
    assert bot.llm.calls == []

    allowed = await say(bot, channel, person(ADMIN_ID, "boss", "0001"), "magbot rm -rf /",
                        pinged=True)
    assert allowed.replies == [("All good!", False)]
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
    assert msg.replies == [("All good!", False)]
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
    assert len(msg.replies) > 1
    assert all(len(text) <= bot.config.bot.max_message_chars for text, _ in msg.replies)
    assert all(mention is False for _, mention in msg.replies)
    joined = " ".join(text for text, _ in msg.replies).split()
    assert joined == bot.llm.text.split()


@pytest.mark.parametrize("error", [LLMError("all providers down"), RuntimeError("bug")])
async def test_model_failure_sends_error_reply(bot, channel, error):
    allow(bot, ALICE_ID, "alice")
    bot.llm.error = error
    msg = await say(bot, channel, person(ALICE_ID, "alice"), "magbot hello", pinged=True)
    assert msg.replies == [(ERROR_REPLY, False)]


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
    (text, _), = msg.replies
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
    assert msg.replies == [("All good!", False)]


async def test_memories_notes_and_overrides_reach_the_prompt(bot, channel):
    allow(bot, ALICE_ID, "alice")
    bot.memory.add(ALICE_ID, "likes tea")
    bot.memory.add(MemoryStore.GLOBAL, "the server is cozy")
    bot.overrides = {"alice#0002": "always answer in rhymes"}
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
    assert msg.replies == [(OPTOUT_FAILED, True)]
    assert bot.memory.for_user(ALICE_ID) == []


async def test_opt_in_failure_is_reported(bot, channel, monkeypatch):
    def broken_allow(user):
        raise OSError("read-only file system")

    monkeypatch.setattr(bot.consent, "allow", broken_allow)
    msg = await say(bot, channel, person(CAROL_ID, "carol"), "magbot opt in")
    assert msg.replies == [(OPTIN_FAILED, True)]


async def test_on_ready_records_the_bot_id(bot):
    bot.config.self_id = ""
    event = SimpleNamespace(me=SimpleNamespace(id=BOT_ID, tag="magbot#0001"),
                            servers=[SimpleNamespace(name="srv", id="SRV")])
    await bot.on_ready(event)
    assert bot.config.self_id == BOT_ID
