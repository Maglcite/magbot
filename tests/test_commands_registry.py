from __future__ import annotations

import pytest

from magbot.commands import REGISTRY, Command, dispatch, parse
from magbot.commands.help import help_text
from magbot.config import Config
from magbot.identity import User

BOT_ID = "01" + "B" * 24
TARGET = "01" + "T" * 24
ADMIN = User("01" + "A" * 24, "boss#1", "boss")
MEMBER = User("01" + "M" * 24, "pal#2", "pal")


class FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.replies: list[str] = []

    async def reply(self, content, mention=True, **kwargs):
        self.replies.append(content)
        return self


class FakeClient:
    def __init__(self):
        self.me = type("Me", (), {"id": BOT_ID})()


@pytest.fixture
def config() -> Config:
    return Config()


def names(config: Config) -> list[str]:
    return config.bot.names


# ── parse ───────────────────────────────────────────────────────────────────

def test_parse_returns_command_spelling_and_args(config):
    cmd, spelling, args = parse("/ban <@01X> spam", names(config))
    assert (cmd.name, spelling, args) == ("ban", "/ban", "<@01X> spam")


def test_parse_ignores_leading_pings_and_names(config):
    cmd, spelling, args = parse(f"<@{BOT_ID}> magbot, /ban <@{TARGET}>", names(config))
    assert cmd.name == "ban"
    assert args == f"<@{TARGET}>"


def test_parse_accepts_multi_word_name_prefix(config):
    cmd, _, args = parse(f"mag bot /kick <@{TARGET}>", names(config))
    assert cmd.name == "kick"
    assert args == f"<@{TARGET}>"


def test_parse_strips_only_the_names_not_inside_words(config):
    assert parse("magbotty /kick x", names(config)) is None


@pytest.mark.parametrize("text", ["/banana", "/bans", "/ban2", "banbanbanbanana", "/purgeall"])
def test_longer_words_do_not_match_a_command(config, text):
    assert parse(text, names(config)) is None


def test_spelling_is_case_insensitive(config):
    cmd, spelling, _ = parse("/BAN <@x>", names(config))
    assert cmd.name == "ban"
    assert spelling == "/BAN"


@pytest.mark.parametrize("text, expected", [
    ("banbanbanban <@T>", "ban"),
    ("banbanbanban", "ban"),
    ("delalldelalldelall <@T>", "del-all"),
    ("purgepurgepurge <@T> confirm", "del-all"),
    ("/delall <@T>", "del-all"),
    ("/del_all", "del-all"),
    ("/purge <@T>", "del-all"),
    ("/del-all <@T>", "del-all"),
    ("/unban <@T>", "unban"),
    ("/help", "help"),
])
def test_alias_spellings(config, text, expected):
    cmd, _, _ = parse(text, names(config))
    assert cmd.name == expected


@pytest.mark.parametrize("text", ["purge the old messages", "ban them all", "help me", "hello"])
def test_plain_words_without_slash_are_not_commands(config, text):
    assert parse(text, names(config)) is None


def test_args_are_stripped_and_empty_when_absent(config):
    _, _, args = parse("/help   ", names(config))
    assert args == ""


# ── dispatch ────────────────────────────────────────────────────────────────

async def test_non_command_is_not_handled(config):
    msg = FakeMessage("just chatting")
    assert await dispatch(FakeClient(), msg, config, MEMBER, is_admin=False) is False
    assert msg.replies == []


async def test_admin_command_refused_for_non_admin(config):
    msg = FakeMessage(f"/ban <@{TARGET}>")
    handled = await dispatch(FakeClient(), msg, config, MEMBER, is_admin=False)
    assert handled is True
    assert msg.replies == ["🚫 Only admins can use that command."]


async def test_word_form_admin_command_refused_for_non_admin(config):
    msg = FakeMessage("purgepurgepurge")
    await dispatch(FakeClient(), msg, config, MEMBER, is_admin=False)
    assert msg.replies == ["🚫 Only admins can use that command."]


async def test_exception_becomes_failed_reply(config, monkeypatch):
    async def boom(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(REGISTRY, "/boom", Command("boom", boom))
    msg = FakeMessage("magbot /boom")
    handled = await dispatch(FakeClient(), msg, config, ADMIN, is_admin=True)
    assert handled is True
    assert msg.replies == ["⚠️ /boom failed: kaboom"]


async def test_exception_text_is_truncated(config, monkeypatch):
    async def boom(ctx):
        raise RuntimeError("x" * 500)

    monkeypatch.setitem(REGISTRY, "/boom", Command("boom", boom))
    msg = FakeMessage("/boom")
    await dispatch(FakeClient(), msg, config, ADMIN, is_admin=True)
    assert msg.replies[0] == "⚠️ /boom failed: " + "x" * 200


async def test_admin_word_form_without_target_gets_usage(config):
    msg = FakeMessage("banbanbanban")
    handled = await dispatch(FakeClient(), msg, config, ADMIN, is_admin=True)
    assert handled is True
    assert msg.replies and msg.replies[0].startswith("⚠️ Usage: `banbanbanban @user [reason]`")


# ── /help ───────────────────────────────────────────────────────────────────

def test_help_hides_admin_commands_from_non_admins():
    text = help_text(is_admin=False)
    assert "`/help`" in text
    assert "`/ban`" not in text
    assert "`/del-all`" not in text
    assert "banbanbanban" not in text


def test_help_lists_each_command_once_with_aliases_for_admins():
    text = help_text(is_admin=True)
    bullets = [line for line in text.splitlines() if line.startswith("•")]
    unique = {id(cmd) for cmd in REGISTRY.values()}
    assert len(bullets) == len(unique)
    assert sum("`/ban`" in line for line in bullets) == 1
    ban_line = next(line for line in bullets if "`/ban`" in line)
    assert "`banbanbanban`" in ban_line
    assert "Ban a user" in ban_line
    del_line = next(line for line in bullets if "`/del-all`" in line)
    for alias in ("`/delall`", "`/del_all`", "`/purge`", "`delalldelalldelall`", "`purgepurgepurge`"):
        assert alias in del_line


async def test_help_command_replies_per_role(config):
    member_msg = FakeMessage("/help")
    await dispatch(FakeClient(), member_msg, config, MEMBER, is_admin=False)
    assert "`/help`" in member_msg.replies[0]
    assert "`/ban`" not in member_msg.replies[0]

    admin_msg = FakeMessage("/help")
    await dispatch(FakeClient(), admin_msg, config, ADMIN, is_admin=True)
    assert "`/ban`" in admin_msg.replies[0]


def test_every_command_has_help_text():
    for cmd in {id(c): c for c in REGISTRY.values()}.values():
        assert cmd.help, f"/{cmd.name} has no help text"
