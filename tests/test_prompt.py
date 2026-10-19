"""Persona loading, overrides and chat-message assembly (offline, tmp files only)."""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace as NS

from magbot.config import BotConfig, Config
from magbot.identity import User
from magbot.prompt import (
    ContextLine,
    FileCache,
    Image,
    build_messages,
    load_overrides,
    load_persona,
)

REPO_PERSONA = Path(__file__).resolve().parents[1] / "persona.md"
ALICE = User("u1", "alice#1001", "alice")


def cfg(**bot) -> Config:
    return Config(bot=BotConfig(**bot))


def memory(id_, text):
    return NS(id=id_, user_id="u1", text=text, created_at=0.0)


def build(**kwargs):
    base = dict(persona="PERSONA", user=ALICE, is_admin=False, can_shell=False, text="hi",
                context=[], memories=[], notes=[])
    base.update(kwargs)
    return build_messages(**base)


def test_persona_placeholders_are_filled(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("I am {bot_name}; files at {file_base_url}/x. Keep {json} braces.")
    config = cfg(names=["zed", "other"], file_base_url="https://files.test",
                 persona_file=str(path))
    assert load_persona(config) == "I am zed; files at https://files.test/x. Keep {json} braces."


def test_missing_persona_uses_fallback_and_warns(tmp_path, caplog):
    config = cfg(names=["zed"], persona_file=str(tmp_path / "missing.md"))
    with caplog.at_level(logging.WARNING):
        text = load_persona(config)
    assert "zed" in text
    assert "{bot_name}" not in text
    assert any("fallback" in r.getMessage() for r in caplog.records)


def test_repo_persona_loads_with_all_placeholders_filled():
    raw = REPO_PERSONA.read_text(encoding="utf-8")
    config = cfg(persona_file=str(REPO_PERSONA), file_base_url="https://files.test")
    text = load_persona(config)
    assert "{bot_name}" not in text and "{file_base_url}" not in text
    assert "{" not in text and "}" not in text
    assert "https://files.test/<filename>" in text
    # Literal URLs would bypass the placeholder, so the file must hold none.
    assert "http" not in raw
    for rule in ("pkill", "killall", "systemctl", "/tmp", "Current speaker", "2000"):
        assert rule in text


def test_overrides_missing_or_unset_is_empty(tmp_path):
    assert load_overrides(cfg()) == {}
    assert load_overrides(cfg(overrides_file=str(tmp_path / "none.json"))) == {}


def test_overrides_are_loaded(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"alice#1001": "speak in rhymes", "u2": "  "}))
    assert load_overrides(cfg(overrides_file=str(path))) == {"alice#1001": "speak in rhymes"}


def test_bad_overrides_file_is_empty(tmp_path, caplog):
    path = tmp_path / "o.json"
    path.write_text("[not, an, object")
    with caplog.at_level(logging.WARNING):
        assert load_overrides(cfg(overrides_file=str(path))) == {}
    assert caplog.records


def test_plain_message_shape():
    messages = build()
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert system.startswith("PERSONA")
    assert "Tag: alice#1001" in system
    assert "Admin: no" in system
    assert "Shell access: no" in system
    assert "Saved memories about this speaker:\n- (none)" in system
    assert "Global notes (about everyone or the server):\n- (none)" in system
    assert "Special rule" not in system
    assert messages[1] == {"role": "user", "content": "[Message from alice#1001]\nhi"}


def test_context_block_marks_bot_lines():
    messages = build(context=[ContextLine("bob#2", "hey", False),
                              ContextLine("magbot", "hello", True)])
    assert messages[1]["content"] == (
        "[Recent messages in this channel]\n"
        "- bob#2: hey\n"
        "- magbot (bot): hello\n\n"
        "[Message from alice#1001]\nhi"
    )


def test_speaker_flags_memories_and_notes_with_ids():
    messages = build(is_admin=True, can_shell=True,
                     memories=[memory(3, "likes cats")],
                     notes=[memory(9, "server rule:\nbe kind")])
    system = messages[0]["content"]
    assert "Admin: yes" in system
    assert "Shell access: yes" in system
    assert "- [3] likes cats" in system
    assert "- [9] server rule: be kind" in system


def test_override_appears_as_special_rule():
    system = build(override="talk like a pirate")[0]["content"]
    assert system.endswith("Special rule for this speaker:\ntalk like a pirate")


def test_images_become_data_url_parts():
    data = b"\x89PNG fake bytes"
    messages = build(images=[Image(data, "image/png", "a.png")])
    content = messages[1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "[Message from alice#1001]\nhi"}
    encoded = base64.b64encode(data).decode("ascii")
    assert content[1] == {"type": "image_url",
                          "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def test_replied_to_block_sits_between_context_and_the_message():
    messages = build(context=[ContextLine("bob#2", "hey", False, id="C1")],
                     replied_to=ContextLine("magbot", "earlier answer", True, id="R1"))
    assert messages[1]["content"] == (
        "[Recent messages in this channel]\n"
        "- bob#2: hey\n\n"
        "[The message they are replying to]\n"
        "magbot (bot): earlier answer\n\n"
        "[Message from alice#1001]\nhi"
    )


def test_replied_to_block_without_context():
    messages = build(replied_to=ContextLine("bob#2", "the original", False))
    assert messages[1]["content"] == (
        "[The message they are replying to]\nbob#2: the original\n\n"
        "[Message from alice#1001]\nhi"
    )


def test_replied_to_text_is_flattened_to_one_line():
    messages = build(replied_to=ContextLine("bob#2", "line one\n\nline two", False))
    assert "bob#2: line one line two\n" in messages[1]["content"]


def set_mtime(path: Path, seconds: int) -> None:
    os.utime(path, ns=(seconds * 10**9, seconds * 10**9))


def test_persona_is_reread_only_when_its_mtime_changes(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("first {bot_name}")
    set_mtime(path, 1_000)
    config = cfg(names=["zed"], persona_file=str(path))
    cache = FileCache()
    assert load_persona(config, cache=cache) == "first zed"

    path.write_text("changed {bot_name}")
    set_mtime(path, 1_000)              # same mtime: the cached text is still used
    assert load_persona(config, cache=cache) == "first zed"

    set_mtime(path, 2_000)              # new mtime: re-read and filled in again
    assert load_persona(config, cache=cache) == "changed zed"


def test_missing_persona_warns_once_until_the_file_changes(tmp_path, caplog):
    path = tmp_path / "p.md"
    config = cfg(names=["zed"], persona_file=str(path))
    cache = FileCache()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert "zed" in load_persona(config, cache=cache)
        warnings = [r for r in caplog.records if "persona file" in r.getMessage()]
        assert len(warnings) == 1

        path.write_text("back {bot_name}")
        set_mtime(path, 1_000)
        assert load_persona(config, cache=cache) == "back zed"

        path.unlink()                   # a new missing state warns again
        load_persona(config, cache=cache)
    warnings = [r for r in caplog.records if "persona file" in r.getMessage()]
    assert len(warnings) == 2


def test_overrides_are_reread_when_their_mtime_changes(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"u1": "one"}))
    set_mtime(path, 1_000)
    config = cfg(overrides_file=str(path))
    cache = FileCache()
    assert load_overrides(config, cache=cache) == {"u1": "one"}

    path.write_text(json.dumps({"u1": "two"}))
    set_mtime(path, 2_000)
    assert load_overrides(config, cache=cache) == {"u1": "two"}


def test_overrides_recover_after_a_bad_edit(tmp_path, caplog):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"u1": "one"}))
    set_mtime(path, 1_000)
    config = cfg(overrides_file=str(path))
    cache = FileCache()
    assert load_overrides(config, cache=cache) == {"u1": "one"}

    path.write_text("{ broken")
    set_mtime(path, 2_000)
    with caplog.at_level(logging.WARNING):
        assert load_overrides(config, cache=cache) == {}
    assert caplog.records

    path.write_text(json.dumps({"u1": "fixed"}))
    set_mtime(path, 3_000)
    assert load_overrides(config, cache=cache) == {"u1": "fixed"}


def test_missing_overrides_file_is_silent(tmp_path, caplog):
    config = cfg(overrides_file=str(tmp_path / "none.json"))
    with caplog.at_level(logging.WARNING):
        assert load_overrides(config, cache=FileCache()) == {}
    assert not caplog.records
