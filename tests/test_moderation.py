from __future__ import annotations

from types import SimpleNamespace

import pytest

from magbot.commands import dispatch
from magbot.commands.moderation import reason_text, server_of, target_user_id
from magbot.config import Config
from magbot.identity import User

ADMIN = User("01" + "A" * 24, "boss#1", "boss")
TARGET = "01" + "T" * 24
BARE_TARGET = "01" + "Z" * 24


class FakeMember:
    def __init__(self):
        self.kicked = 0

    async def kick(self):
        self.kicked += 1


class FakeServer:
    def __init__(self, *, members=None, fail: str | None = None):
        self.members = members or {}
        self.fail = fail
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self.fail:
            raise RuntimeError(self.fail)

    async def ban(self, user, reason=None):
        self.calls.append(("ban", user, reason))
        self._maybe_fail()

    async def unban(self, user):
        self.calls.append(("unban", user))
        self._maybe_fail()

    async def kick(self, user):
        self.calls.append(("kick", user))
        self._maybe_fail()

    async def fetch_member(self, user):
        self.calls.append(("fetch_member", user))
        self._maybe_fail()
        return self.members.get(user)


class FakeChannel:
    def __init__(self, server=None):
        self.id = "CHAN"
        self.server = server

    async def history(self, **kwargs):
        raise AssertionError("moderation must not read history")


class FakeMessage:
    def __init__(self, content: str, *, server=None, channel=None):
        self.id = "MSG"
        self.content = content
        self.author_id = ADMIN.id
        self.server = server
        self.channel = channel or FakeChannel()
        self.replies: list[str] = []

    async def reply(self, content, mention=True, **kwargs):
        self.replies.append(content)
        return self


class FakeClient:
    def __init__(self):
        self.me = SimpleNamespace(id="01" + "B" * 24)


async def run(content: str, server=None, channel=None) -> FakeMessage:
    msg = FakeMessage(content, server=server, channel=channel)
    await dispatch(FakeClient(), msg, Config(), ADMIN, is_admin=True)
    return msg


# ── helpers ─────────────────────────────────────────────────────────────────

def test_target_from_mention():
    assert target_user_id(f"<@{TARGET}> spam") == TARGET


def test_target_from_bare_stoat_id():
    assert target_user_id(f"{BARE_TARGET} spam") == BARE_TARGET


def test_target_rejects_short_or_non_ulid_ids():
    assert target_user_id("01SHORT") is None
    assert target_user_id("02" + "T" * 24) is None
    assert target_user_id("01" + "I" * 24) is None   # I is not Crockford base32


def test_mention_wins_over_bare_id():
    assert target_user_id(f"{BARE_TARGET} <@{TARGET}>") == TARGET


def test_reason_drops_mentions_and_ids():
    assert reason_text(f"<@{TARGET}> spamming   links") == "spamming   links"
    assert reason_text(f"{BARE_TARGET} raid") == "raid"
    assert reason_text(f"<@{TARGET}>") is None


def test_server_of_falls_back_to_channel():
    server = FakeServer()
    msg = SimpleNamespace(server=None, channel=SimpleNamespace(server=server))
    assert server_of(msg) is server


def test_server_of_is_none_in_dms():
    msg = SimpleNamespace(server=None, channel=SimpleNamespace())
    assert server_of(msg) is None


# ── /ban ────────────────────────────────────────────────────────────────────

async def test_ban_without_reason():
    server = FakeServer()
    msg = await run(f"/ban <@{TARGET}>", server=server)
    assert server.calls == [("ban", TARGET, None)]
    assert msg.replies == [f"🔨 Banned <@{TARGET}>."]


async def test_ban_with_reason_and_bare_id():
    server = FakeServer()
    msg = await run(f"/ban {BARE_TARGET} raid bot", server=server)
    assert server.calls == [("ban", BARE_TARGET, "raid bot")]
    assert msg.replies == [f"🔨 Banned <@{BARE_TARGET}> — reason: raid bot."]


async def test_ban_word_form_alias():
    server = FakeServer()
    msg = await run(f"banbanbanban <@{TARGET}> spam", server=server)
    assert server.calls == [("ban", TARGET, "spam")]
    assert msg.replies == [f"🔨 Banned <@{TARGET}> — reason: spam."]


async def test_ban_usage_without_target():
    server = FakeServer()
    msg = await run("/ban just words", server=server)
    assert server.calls == []
    assert msg.replies == [
        "⚠️ Usage: `banbanbanban @user [reason]` (or `/ban @user [reason]`) — "
        "mention the user or paste their ID."
    ]


async def test_ban_needs_a_server():
    msg = await run(f"/ban <@{TARGET}>", server=None, channel=FakeChannel(server=None))
    assert msg.replies == [
        "⚠️ Can't determine the server from this message. Use this in a server channel."
    ]


async def test_ban_uses_channel_server_when_message_has_none():
    server = FakeServer()
    msg = await run(f"/ban <@{TARGET}>", server=None, channel=FakeChannel(server=server))
    assert server.calls == [("ban", TARGET, None)]
    assert msg.replies == [f"🔨 Banned <@{TARGET}>."]


async def test_ban_failure_is_reported():
    server = FakeServer(fail="missing Ban Members")
    msg = await run(f"/ban <@{TARGET}>", server=server)
    assert msg.replies == ["⚠️ /ban failed: missing Ban Members"]


async def test_ban_failure_text_is_truncated():
    server = FakeServer(fail="e" * 500)
    msg = await run(f"/ban <@{TARGET}>", server=server)
    assert msg.replies == ["⚠️ /ban failed: " + "e" * 200]


# ── /kick ───────────────────────────────────────────────────────────────────

async def test_kick_member_found_uses_member_kick():
    member = FakeMember()
    server = FakeServer(members={TARGET: member})
    msg = await run(f"/kick <@{TARGET}>", server=server)
    assert member.kicked == 1
    assert ("kick", TARGET) not in server.calls
    assert msg.replies == [f"👢 Kicked <@{TARGET}>."]


async def test_kick_member_not_found_uses_server_kick():
    server = FakeServer(members={})
    msg = await run(f"/kick <@{TARGET}>", server=server)
    assert server.calls == [("fetch_member", TARGET), ("kick", TARGET)]
    assert msg.replies == [f"👢 Kicked <@{TARGET}>."]


async def test_kick_failure_is_reported_as_kick():
    server = FakeServer(fail="no permission")
    msg = await run(f"/kick <@{TARGET}>", server=server)
    assert msg.replies == ["⚠️ /kick failed: no permission"]


async def test_kick_usage_without_target():
    server = FakeServer()
    msg = await run("/kick", server=server)
    assert server.calls == []
    assert msg.replies[0].startswith("⚠️ Usage:")


# ── /unban ──────────────────────────────────────────────────────────────────

async def test_unban_success():
    server = FakeServer()
    msg = await run(f"/unban <@{TARGET}>", server=server)
    assert server.calls == [("unban", TARGET)]
    assert msg.replies == [f"✅ Unbanned <@{TARGET}>."]


async def test_unban_failure_is_reported_as_unban():
    server = FakeServer(fail="not banned")
    msg = await run(f"/unban <@{TARGET}>", server=server)
    assert msg.replies == ["⚠️ /unban failed: not banned"]


async def test_unban_with_bare_id_after_leading_name():
    server = FakeServer()
    msg = await run(f"magbot /unban {BARE_TARGET}", server=server)
    assert server.calls == [("unban", BARE_TARGET)]
    assert msg.replies == [f"✅ Unbanned <@{BARE_TARGET}>."]


@pytest.mark.parametrize("content", ["/ban <@x>", "/kick <@x>", "/unban <@x>"])
async def test_moderation_commands_are_admin_only(content):
    msg = FakeMessage(content, server=FakeServer())
    await dispatch(FakeClient(), msg, Config(), User("01" + "M" * 24, "pal#2", "pal"), is_admin=False)
    assert msg.replies == ["🚫 Only admins can use that command."]
