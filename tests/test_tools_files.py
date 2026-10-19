"""Tests for magbot/tools/files.py: names, traversal, size caps, symlinks, URLs."""
from __future__ import annotations

import os

import pytest

from magbot.config import Config
from magbot.identity import User
from magbot.tools import ToolContext, ToolError, call
from magbot.tools import files as mfiles


def make_ctx(tmp_path, *, admin: bool = False) -> ToolContext:
    cfg = Config()
    cfg.bot.www_dir = str(tmp_path)
    cfg.bot.file_base_url = "https://files.test/"
    return ToolContext(config=cfg, user=User("u1", "alice#1001", "Alice"), is_admin=admin,
                       can_shell=admin, channel_id="chan", memory=None)


@pytest.mark.parametrize("name", ["a", "notes.txt", "my-file_1.tar.gz", "A" * 100])
def test_valid_names(name):
    assert mfiles.check_name(name) == name


@pytest.mark.parametrize("name", [
    "", ".hidden", "../etc/passwd", "a/b", "a\\b", "..", ".", "a b", "a\x00b",
    "name\n", "A" * 101, "ünï.txt", None, 7,
])
def test_bad_names(name):
    with pytest.raises(ToolError, match="invalid file name"):
        mfiles.check_name(name)


async def test_write_then_read_roundtrip_and_url(tmp_path):
    ctx = make_ctx(tmp_path)
    out = await mfiles.write_file(ctx, name="hello.txt", content="hi there")
    assert out == "Saved hello.txt (8 bytes): https://files.test/hello.txt"
    assert (tmp_path / "hello.txt").read_text() == "hi there"
    assert ctx.outputs == [str(tmp_path / "hello.txt")]
    assert await mfiles.read_file(ctx, name="hello.txt") == "hi there"


async def test_write_overwrites_existing_file(tmp_path):
    ctx = make_ctx(tmp_path)
    await mfiles.write_file(ctx, name="a.txt", content="long original text")
    await mfiles.write_file(ctx, name="a.txt", content="new")
    assert (tmp_path / "a.txt").read_text() == "new"


async def test_write_refuses_oversize_content(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="1 MB"):
        await mfiles.write_file(ctx, name="big.txt", content="x" * (mfiles.MAX_WRITE_BYTES + 1))
    assert not (tmp_path / "big.txt").exists()
    await mfiles.write_file(ctx, name="ok.txt", content="x" * mfiles.MAX_WRITE_BYTES)
    assert (tmp_path / "ok.txt").stat().st_size == mfiles.MAX_WRITE_BYTES


async def test_traversal_names_are_refused_and_nothing_written(tmp_path):
    ctx = make_ctx(tmp_path / "www")
    for name in ["../escape.txt", "sub/x.txt", "/abs.txt", "..", ".env"]:
        with pytest.raises(ToolError, match="invalid file name"):
            await mfiles.write_file(ctx, name=name, content="x")
    assert not (tmp_path / "escape.txt").exists()


async def test_symlink_target_is_refused_for_read_write_and_delete(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    web = tmp_path / "www"
    web.mkdir()
    (web / "link.txt").symlink_to(secret)
    ctx = make_ctx(web, admin=True)

    with pytest.raises(ToolError, match="symbolic link"):
        await mfiles.write_file(ctx, name="link.txt", content="overwrite")
    with pytest.raises(ToolError, match="symbolic link"):
        await mfiles.read_file(ctx, name="link.txt")
    with pytest.raises(ToolError, match="symbolic link"):
        await mfiles.delete_file(ctx, name="link.txt")
    assert secret.read_text() == "top secret"


async def test_symlink_swapped_in_after_check_is_not_followed(tmp_path, monkeypatch):
    # Simulate the race: the name looks fine when checked, then becomes a symlink
    # just before the open. O_NOFOLLOW must stop the write from following it.
    secret = tmp_path / "secret.txt"
    secret.write_text("keep me")
    web = tmp_path / "www"
    web.mkdir()
    ctx = make_ctx(web)
    real_path = mfiles._path

    def check_then_swap(c, name):
        p = real_path(c, name)
        (web / "race.txt").symlink_to(secret)
        return p

    monkeypatch.setattr(mfiles, "_path", check_then_swap)
    with pytest.raises(ToolError, match="could not write"):
        await mfiles.write_file(ctx, name="race.txt", content="pwned")
    assert secret.read_text() == "keep me"


async def test_read_missing_and_binary_files(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(ToolError, match="no file named"):
        await mfiles.read_file(ctx, name="missing.txt")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01binary")
    with pytest.raises(ToolError, match="binary"):
        await mfiles.read_file(ctx, name="data.bin")


async def test_read_is_capped_to_fetch_max_chars(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.tools.fetch_max_chars = 50
    (tmp_path / "long.txt").write_text("y" * 500)
    out = await mfiles.read_file(ctx, name="long.txt")
    assert out.startswith("y" * 50) and "cut at 50 characters" in out


async def test_empty_file_reads_as_placeholder(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "empty.txt").write_text("")
    assert await mfiles.read_file(ctx, name="empty.txt") == "(empty file)"


async def test_list_files_shows_names_sizes_and_urls(tmp_path):
    ctx = make_ctx(tmp_path)
    assert await mfiles.list_files(ctx) == "No files yet."
    (tmp_path / "b.txt").write_text("12345")
    (tmp_path / "a.txt").write_text("1")
    (tmp_path / "subdir").mkdir()
    assert await mfiles.list_files(ctx) == (
        "a.txt (1 bytes) https://files.test/a.txt\n"
        "b.txt (5 bytes) https://files.test/b.txt")


async def test_list_files_skips_symlinks(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("x")
    web = tmp_path / "www"
    web.mkdir()
    (web / "link.txt").symlink_to(secret)
    ctx = make_ctx(web)
    assert await mfiles.list_files(ctx) == "No files yet."


async def test_delete_file_removes_and_reports_missing(tmp_path):
    ctx = make_ctx(tmp_path, admin=True)
    (tmp_path / "gone.txt").write_text("x")
    assert await mfiles.delete_file(ctx, name="gone.txt") == "Deleted gone.txt."
    assert not (tmp_path / "gone.txt").exists()
    with pytest.raises(ToolError, match="no file named"):
        await mfiles.delete_file(ctx, name="gone.txt")


async def test_delete_file_is_admin_only_in_the_registry(tmp_path):
    user_ctx = make_ctx(tmp_path, admin=False)
    (tmp_path / "keep.txt").write_text("x")
    out = await call("delete_file", '{"name": "keep.txt"}', user_ctx)
    assert out == "error: tool 'delete_file' is not available"
    assert (tmp_path / "keep.txt").exists()

    admin_ctx = make_ctx(tmp_path, admin=True)
    assert await call("delete_file", '{"name": "keep.txt"}', admin_ctx) == "Deleted keep.txt."


async def test_registry_call_reports_bad_names_as_error_strings(tmp_path):
    ctx = make_ctx(tmp_path)
    out = await call("write_file", '{"name": "../x", "content": "y"}', ctx)
    assert out.startswith("error: invalid file name")


async def test_write_file_is_available_to_everyone(tmp_path):
    ctx = make_ctx(tmp_path)
    out = await call("write_file", '{"name": "via-registry.txt", "content": "ok"}', ctx)
    assert out == "Saved via-registry.txt (2 bytes): https://files.test/via-registry.txt"
    assert os.path.exists(tmp_path / "via-registry.txt")


async def test_read_file_refuses_fifo_without_hanging(tmp_path):
    import asyncio
    import os
    from magbot import tools
    from magbot.config import Config
    from magbot.identity import User
    from magbot.tools import ToolContext
    cfg = Config()
    cfg.bot.www_dir = str(tmp_path)
    os.mkfifo(tmp_path / "pipe")
    ctx = ToolContext(config=cfg, user=User("1", "a#0001", "a"), is_admin=True,
                      can_shell=False, channel_id="c", memory=None)
    result = await asyncio.wait_for(tools.call("read_file", {"name": "pipe"}, ctx), 5)
    assert result.startswith("error:")
