"""Tests for magbot/tools/shell.py: output, exit codes, timeouts, safety and permissions."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import magbot.safety as safety
from magbot.config import Config
from magbot.identity import User
from magbot.tools import ToolContext, ToolError, call
from magbot.tools import shell as mshell


def make_ctx(tmp_path, *, admin: bool = False, can_shell: bool = True) -> ToolContext:
    cfg = Config()
    cfg.bot.www_dir = str(tmp_path)
    return ToolContext(config=cfg, user=User("u1", "alice#1001", "Alice"), is_admin=admin,
                       can_shell=can_shell or admin, channel_id="chan", memory=None)


async def test_output_and_exit_code(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    assert await mshell.run_shell(ctx, command="echo hello") == "exit code 0\nhello"
    assert await mshell.run_shell(ctx, command="echo oops; exit 3") == "exit code 3\noops"
    assert await mshell.run_shell(ctx, command="true") == "exit code 0\n(no output)"


async def test_stderr_is_combined_with_stdout(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    out = await mshell.run_shell(ctx, command="echo out; echo err 1>&2")
    assert out == "exit code 0\nout\nerr"


async def test_runs_in_the_web_folder(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    (tmp_path / "marker.txt").write_text("here")
    out = await mshell.run_shell(ctx, command="cat marker.txt")
    assert out == "exit code 0\nhere"


async def test_ansi_escape_codes_are_stripped(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    out = await mshell.run_shell(ctx, command=r"printf '\033[31mred\033[0m \033]0;title\007ok\n'")
    assert out == "exit code 0\nred ok"


async def test_output_is_capped(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    ctx.config.tools.shell_max_output = 100
    out = await mshell.run_shell(ctx, command="yes abcdefgh | head -c 200000")
    assert "[... output cut]" in out
    assert len(out) < 200


async def test_empty_and_overlong_commands_are_refused(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    with pytest.raises(ToolError, match="empty"):
        await mshell.run_shell(ctx, command="   ")
    with pytest.raises(ToolError, match="over"):
        await mshell.run_shell(ctx, command="echo " + "x" * 3000)


def _alive(pid: int) -> bool:
    """True if the process exists and isn't a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


async def test_timeout_kills_the_whole_process_group(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    ctx.config.tools.shell_timeout = 1.0
    command = "sleep 30 & echo $! > child.pid; sleep 30"
    with pytest.raises(ToolError, match="timed out"):
        await mshell.run_shell(ctx, command=command)

    child = int((tmp_path / "child.pid").read_text())
    for _ in range(60):
        if not _alive(child):
            break
        await asyncio.sleep(0.05)
    assert not _alive(child), "background child survived the timeout"


async def test_nonzero_exit_is_not_an_error(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    out = await mshell.run_shell(ctx, command="exit 7")
    assert out == "exit code 7\n(no output)"


async def test_non_admin_is_refused_when_safety_says_no(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "is_safe", lambda text: False)
    ctx = make_ctx(tmp_path, admin=False, can_shell=True)
    with pytest.raises(ToolError, match="safety filter"):
        await mshell.run_shell(ctx, command="touch should-not-exist")
    assert not (tmp_path / "should-not-exist").exists()


async def test_non_admin_runs_when_safety_allows(tmp_path, monkeypatch):
    seen = []

    def allow(text):
        seen.append(text)
        return True

    monkeypatch.setattr(safety, "is_safe", allow)
    ctx = make_ctx(tmp_path, admin=False, can_shell=True)
    assert await mshell.run_shell(ctx, command="echo fine") == "exit code 0\nfine"
    assert seen == ["echo fine"]


async def test_admin_bypasses_safety_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "is_safe", lambda text: False)
    ctx = make_ctx(tmp_path, admin=True)
    assert await mshell.run_shell(ctx, command="echo admin") == "exit code 0\nadmin"


async def test_shell_is_hidden_from_users_without_permission(tmp_path):
    ctx = make_ctx(tmp_path, admin=False, can_shell=False)
    out = await call("run_shell", '{"command": "echo hi"}', ctx)
    assert out == "error: tool 'run_shell' is not available"


async def test_registry_path_runs_for_shell_users(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "is_safe", lambda text: True)
    ctx = make_ctx(tmp_path, admin=False, can_shell=True)
    assert await call("run_shell", '{"command": "echo via registry"}', ctx) == \
        "exit code 0\nvia registry"


async def test_child_environment_hides_api_keys_and_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGBOT_TEST_PROVIDER_ENV", "sekrit-provider-value")
    monkeypatch.setenv("SOMETHING_PASSWORD", "sekrit-password-value")
    monkeypatch.setenv("STOAT_BOT_TOKEN", "sekrit-token-value")
    ctx = make_ctx(tmp_path, admin=True)
    ctx.config.llm.providers[0].api_key_env = "MAGBOT_TEST_PROVIDER_ENV"
    out = await mshell.run_shell(ctx, command="env")
    assert "sekrit" not in out
    assert "MAGBOT_TEST_PROVIDER_ENV" not in out
    assert "PATH=" in out
