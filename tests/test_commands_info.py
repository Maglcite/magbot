"""Tests for magbot/commands/info.py: /status (admins) and /forget-me. Offline; files only under tmp_path."""
from __future__ import annotations

import time

import pytest

from magbot.commands import dispatch
from magbot.commands.help import help_text
from magbot.commands.info import format_uptime
from magbot.config import Config, ProviderConfig
from magbot.consent import Consent
from magbot.identity import User
from magbot.memory import MemoryStore

ADMIN = User("01" + "A" * 24, "boss#1", "boss")
MEMBER = User("01" + "M" * 24, "pal#2", "pal")
OTHER = User("01" + "O" * 24, "other#3", "other")


class FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.replies: list[str] = []

    async def reply(self, content, mention=True, **kwargs):
        self.replies.append(content)
        return self


class FakeClient:
    def __init__(self, config, memory=None, consent=None, started_at=None, stats=None):
        self.config = config
        self.memory = memory
        self.consent = consent
        self.started_at = started_at
        self.stats = stats if stats is not None else {}


class StoreWithPending(MemoryStore):
    def pending_count(self) -> int:
        return 0


class BrokenStore:
    GLOBAL = "*"

    def forget_user(self, user_id, tag=""):
        raise RuntimeError("disk full")


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config()
    cfg.bot.data_dir = str(tmp_path / "data")
    cfg.bot.www_dir = str(tmp_path / "www")
    cfg.llm.providers = [
        ProviderConfig(name="primary", model="model-a", api_key_env="TEST_PRIMARY_KEY",
                       base_url="https://secret.example/v1"),
        ProviderConfig(name="backup", model="model-b", api_key_env="TEST_BACKUP_KEY"),
    ]
    return cfg


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def consent(config) -> Consent:
    c = Consent(config)
    c.allow(MEMBER)
    return c


# ── format_uptime ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds, expected", [
    (0, "0s"),
    (45, "45s"),
    (125, "2m"),
    (3 * 3600 + 12 * 60, "3h 12m"),
    (2 * 86400 + 4 * 3600 + 5, "2d 4h"),
    (-5, "0s"),
])
def test_format_uptime(seconds, expected):
    assert format_uptime(seconds) == expected


# ── /status ─────────────────────────────────────────────────────────────────

async def test_status_is_refused_for_non_admins(config, store, consent):
    client = FakeClient(config, store, consent, started_at=time.time())
    msg = FakeMessage("/status")
    assert await dispatch(client, msg, config, MEMBER, is_admin=False) is True
    assert msg.replies == ["🚫 Only admins can use that command."]


async def test_status_shows_uptime_providers_and_counts(config, consent):
    s = StoreWithPending(":memory:")
    try:
        s.add(MEMBER.id, "likes tea")
        s.add(OTHER.id, "likes coffee")
        s.add(MemoryStore.GLOBAL, "the server is loud on fridays")
        client = FakeClient(
            config, s, consent,
            started_at=time.time() - (3 * 3600 + 12 * 60 + 5),
            stats={"replies": 14, "errors": 2, "rate_limited": 1,
                   "prompt_tokens": 12345, "completion_tokens": 678},
        )
        msg = FakeMessage("/status")
        assert await dispatch(client, msg, config, ADMIN, is_admin=True) is True
        text = msg.replies[0]
    finally:
        s.close()

    assert "**uptime:** 3h 12m" in text
    assert "`primary: model-a` → `backup: model-b`" in text
    assert "**search:** duckduckgo" in text
    assert "14 replies, 2 errors, 1 rate-limited" in text
    assert "12,345 in, 678 out" in text
    assert "**memories:** 3 stored, 1 of them global notes, 0 pending" in text
    assert "**consent:** 1 by id, 1 by tag opted in; 0 excluded" in text


async def test_status_leaks_no_secrets_or_paths(config, store, consent, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PRIMARY_KEY", "sk-secret-123")
    client = FakeClient(config, store, consent, started_at=time.time())
    msg = FakeMessage("/status")
    await dispatch(client, msg, config, ADMIN, is_admin=True)
    text = msg.replies[0]
    assert "sk-secret" not in text
    assert "secret.example" not in text
    assert "TEST_PRIMARY_KEY" not in text
    assert str(tmp_path) not in text
    assert MEMBER.id not in text and "pal#2" not in text


async def test_status_marks_unavailable_data_with_question_marks(config):
    # A client with none of the expected attributes still gets a reply.
    msg = FakeMessage("/status")
    assert await dispatch(object(), msg, config, ADMIN, is_admin=True) is True
    text = msg.replies[0]
    assert "⚠️" not in text
    assert "**uptime:** ?" in text
    assert "**memories:** ?" in text
    assert "**consent:** ?" in text
    assert "`primary: model-a`" in text    # providers come from the config fallback


async def test_pending_is_unknown_when_the_store_has_no_count(config, store, consent, monkeypatch):
    monkeypatch.delattr(MemoryStore, "pending_count", raising=False)
    client = FakeClient(config, store, consent, started_at=time.time())
    msg = FakeMessage("/status")
    await dispatch(client, msg, config, ADMIN, is_admin=True)
    assert "0 pending" not in msg.replies[0]
    assert "? pending" in msg.replies[0]


async def test_web_search_disabled_is_shown(config, store, consent):
    config.tools.disabled = ["web_search"]
    client = FakeClient(config, store, consent, started_at=time.time())
    msg = FakeMessage("/status")
    await dispatch(client, msg, config, ADMIN, is_admin=True)
    assert "**search:** off" in msg.replies[0]


# ── /forget-me ──────────────────────────────────────────────────────────────

async def test_forget_me_deletes_only_the_callers_memories(config, store):
    store.add(MEMBER.id, "likes tea")
    store.add(MEMBER.id, "has a dog")
    store.add(OTHER.id, "likes coffee")
    store.add(MemoryStore.GLOBAL, "fridays are loud")
    msg = FakeMessage("/forget-me")
    assert await dispatch(FakeClient(config, memory=store), msg, config, MEMBER, is_admin=False) is True
    assert msg.replies == ["done~ i forgot 2 things about you 🫧"]
    assert store.for_user(MEMBER.id) == []
    assert [m.text for m in store.for_user(OTHER.id)] == ["likes coffee"]
    assert [m.text for m in store.for_user(MemoryStore.GLOBAL)] == ["fridays are loud"]


async def test_forget_me_singular_wording(config, store):
    store.add(MEMBER.id, "likes tea")
    msg = FakeMessage("/forget-me")
    await dispatch(FakeClient(config, memory=store), msg, config, MEMBER, is_admin=False)
    assert msg.replies == ["done~ i forgot 1 thing about you 🫧"]


async def test_forgetme_alias_with_nothing_saved(config, store):
    msg = FakeMessage("forgetme")
    await dispatch(FakeClient(config, memory=store), msg, config, MEMBER, is_admin=False)
    assert msg.replies == ["i didn't have anything saved about you ✨"]


async def test_forget_me_failure_gets_a_short_reply(config):
    msg = FakeMessage("/forget-me")
    await dispatch(FakeClient(config, memory=BrokenStore()), msg, config, MEMBER, is_admin=False)
    assert len(msg.replies) == 1
    assert msg.replies[0].startswith("⚠️ i couldn't clear your memories")
    assert "disk full" not in msg.replies[0]


async def test_forget_me_without_a_store_gets_a_failure_reply(config):
    msg = FakeMessage("/forget-me")
    await dispatch(object(), msg, config, MEMBER, is_admin=False)
    assert msg.replies[0].startswith("⚠️ i couldn't clear your memories")


def test_forget_me_needs_the_whole_word():
    from magbot.commands import parse
    assert parse("/forget-mex", Config().bot.names) is None


# ── /help ───────────────────────────────────────────────────────────────────

def test_help_lists_both_commands_in_the_right_places():
    member_help = help_text(is_admin=False)
    assert "`/forget-me`" in member_help and "`forgetme`" in member_help
    assert "`/status`" not in member_help
    admin_help = help_text(is_admin=True)
    status_line = next(line for line in admin_help.splitlines() if "`/status`" in line)
    assert "Show bot health" in status_line
    assert "(admins only)" in status_line
