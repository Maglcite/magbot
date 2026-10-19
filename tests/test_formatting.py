from __future__ import annotations

from magbot.config import Config
from magbot.formatting import format_thinking, sanitize_markdown, split_message, strip_address


def cfg(mode: str = "blockquote", max_lines: int = 15, max_chars: int = 1200) -> Config:
    config = Config()
    config.bot.thinking_mode = mode
    config.bot.thinking_max_lines = max_lines
    config.bot.thinking_max_chars = max_chars
    return config


# ── sanitize_markdown ───────────────────────────────────────────────────────

def test_sanitize_swaps_tildes_outside_code():
    assert sanitize_markdown("~o-oh! w-well~ :3") == "～o-oh! w-well～ :3"


def test_sanitize_keeps_tildes_in_code_spans_and_blocks():
    text = "use `a~b` here ~x~\n```\nls ~/dir\n```\n~y~"
    out = sanitize_markdown(text)
    assert "`a~b`" in out
    assert "ls ~/dir" in out
    assert "～x～" in out and "～y～" in out


def test_sanitize_without_tilde_is_identity():
    assert sanitize_markdown("plain text") == "plain text"


# ── format_thinking ─────────────────────────────────────────────────────────

def test_thinking_hidden_when_off_or_empty():
    assert format_thinking("some thoughts", cfg("off")) == ""
    assert format_thinking("   ", cfg("blockquote")) == ""
    assert format_thinking("", cfg("blockquote")) == ""


def test_thinking_blockquote_quotes_lines_and_blank_lines():
    out = format_thinking("first\n\nsecond", cfg("blockquote"))
    assert out == "> 💭 **Thinking**\n> first\n>\n> second"


def test_thinking_spoiler_wraps_block():
    out = format_thinking("hmm", cfg("spoiler"))
    assert out == "||\n> 💭 **Thinking**\n> hmm\n||"


def test_thinking_plain_has_no_quoting():
    out = format_thinking("hmm\nok", cfg("plain"))
    assert out == "💭 **Thinking**\nhmm\nok"


def test_thinking_caps_lines_with_note():
    out = format_thinking("a\nb\nc\nd\ne", cfg("plain", max_lines=2))
    assert out == "💭 **Thinking**\na\nb\n_… 3 more lines_"


def test_thinking_caps_chars_with_ellipsis():
    out = format_thinking("x" * 50, cfg("plain", max_chars=10))
    assert out == "💭 **Thinking**\n" + "x" * 10 + "…"


def test_thinking_sanitizes_tildes():
    out = format_thinking("~hi~", cfg("plain"))
    assert "～hi～" in out


# ── split_message ───────────────────────────────────────────────────────────

def test_split_short_text_is_untouched():
    assert split_message("hello", 10) == ["hello"]
    assert split_message("x" * 10, 10) == ["x" * 10]


def test_split_prefers_paragraph_break_in_second_half():
    text = "a" * 40 + "\n\n" + "b" * 40 + "\n\n" + "c" * 40
    chunks = split_message(text, 60)
    assert chunks == ["a" * 40, "b" * 40, "c" * 40]


def test_split_falls_back_to_space():
    text = "word " * 30
    chunks = split_message(text.strip(), 50)
    assert all(len(c) <= 50 for c in chunks)
    assert " ".join(chunks).split() == text.split()
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)


def test_split_hard_cuts_when_no_break_exists():
    text = "z" * 25
    assert split_message(text, 10) == ["z" * 10, "z" * 10, "z" * 5]


def test_split_respects_limit_for_long_text():
    text = ("lorem ipsum dolor sit amet\n\n" * 200).strip()
    chunks = split_message(text, 1600)
    assert len(chunks) > 1
    assert all(len(c) <= 1600 for c in chunks)
    assert "".join(chunks).replace("\n", "").replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_split_avoids_cutting_inside_fence_when_outside_break_exists():
    # Window is [0, 100). The paragraph break at index 80 is inside the code
    # block, but the plain space at index 55 is outside it and in the second half.
    text = "a" * 55 + " " + "```\n" + "b" * 20 + "\n\n" + "c" * 30 + "\n```\n" + "e" * 40
    chunks = split_message(text, 100)
    assert chunks[0] == "a" * 55
    assert all(len(c) <= 100 for c in chunks)
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_closes_and_reopens_fence_on_forced_cut():
    code = "```python\n" + "x" * 300 + "\n```"
    chunks = split_message(code, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    for c in chunks:
        assert c.count("```") % 2 == 0, c          # every chunk is a complete block
    assert chunks[0].startswith("```python\n")
    assert chunks[0].endswith("\n```")
    assert chunks[1].startswith("```python\n")     # reopened with the same language tag
    assert chunks[-1].endswith("\n```")


def test_split_outside_fence_text_keeps_no_fence_markers():
    text = "hello " * 50
    chunks = split_message(text, 40)
    assert all("```" not in c for c in chunks)


def test_split_rejects_non_positive_limit():
    try:
        split_message("abc", 0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ── strip_address ───────────────────────────────────────────────────────────

NAMES = ["magbot", "mag bot", "mag-bot", "fembot"]


def test_strip_address_removes_pings_and_names():
    assert strip_address("<@01ABC> magbot, what is up?", NAMES) == ", what is up?"


def test_strip_address_names_are_whole_words_and_case_insensitive():
    assert strip_address("MagBot help magbotx xmagbot", NAMES) == "help magbotx xmagbot"


def test_strip_address_handles_name_variants():
    assert strip_address("hi mag bot and Mag-Bot and fembot", NAMES) == "hi and and"


def test_strip_address_collapses_leftover_whitespace():
    assert strip_address("  hey   magbot\t\tthere  ", NAMES) == "hey there"


def test_strip_address_keeps_line_breaks():
    assert strip_address("magbot one\n\n\n\ntwo", NAMES) == "one\n\ntwo"


def test_split_message_never_returns_blank_chunks():
    from magbot.formatting import split_message
    assert split_message(" \n", 38) == []
    assert split_message("", 10) == []
    for chunk in split_message("a\n\n \n\n" * 50, 7):
        assert chunk.strip()
