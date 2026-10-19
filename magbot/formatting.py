"""Reply formatting: markdown fixes, thinking traces, splitting, address stripping."""
from __future__ import annotations

import re

from .config import Config

# Stoat renders single-tilde strikethrough, so the persona's "~" tics would strike
# out text. Swap them for the look-alike fullwidth tilde outside code.
_FW_TILDE = "～"
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)

_THINK_HEADER = "💭 **Thinking**"
_FENCE = "```"
_CLOSE_FENCE = "\n```"


def sanitize_markdown(text: str) -> str:
    """Replace "~" with a fullwidth tilde outside code spans and code blocks."""
    if "~" not in text:
        return text
    parts = _CODE_SPLIT.split(text)
    for i in range(0, len(parts), 2):  # even indexes are outside code
        parts[i] = parts[i].replace("~", _FW_TILDE)
    return "".join(parts)


def format_thinking(reasoning: str, config: Config) -> str:
    """Render reasoning as a Stoat markdown block, or "" when it should be hidden.

    Shown only when the model reasoned and bot.thinking_mode is not "off". The
    text is capped to thinking_max_lines / thinking_max_chars, with a note saying
    how much was dropped.
    """
    mode = config.bot.thinking_mode.strip().lower()
    if mode == "off" or not reasoning or not reasoning.strip():
        return ""
    text = sanitize_markdown(reasoning.strip())
    max_chars = config.bot.thinking_max_chars
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    lines = text.splitlines()
    dropped = 0
    max_lines = config.bot.thinking_max_lines
    if max_lines > 0 and len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[:max_lines]
    body = "\n".join(lines).strip("\n")
    if mode == "plain":
        block = f"{_THINK_HEADER}\n{body}"
        if dropped:
            block += f"\n_… {dropped} more lines_"
        return block
    # blockquote and spoiler share the quoted body
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in body.splitlines())
    block = f"> {_THINK_HEADER}\n{quoted}"
    if dropped:
        block += f"\n> _… {dropped} more lines_"
    if mode == "spoiler":
        # The header stays visible inside the spoiler so readers know what is hidden.
        block = f"||\n{block}\n||"
    return block


def split_message(text: str, limit: int) -> list[str]:
    """Split `text` into chunks of at most `limit` characters.

    Prefers breaking at a blank line, then at a space, in the second half of the
    window; otherwise cuts hard at the limit. Breaks outside ``` blocks are
    preferred. If a cut has to land inside a block, the block is closed at the
    end of the chunk and reopened (same opening line) at the start of the next.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if text.strip() else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max(1, _split_point(rest, limit))
        prefix = suffix = ""
        if _in_fence(rest, cut):
            cut = max(1, min(cut, limit - len(_CLOSE_FENCE)))  # room for the closing fence
            opener = _fence_opener(rest, cut) if _in_fence(rest, cut) else ""
            if opener and len(opener) + 1 < cut:  # the reopened rest must still shrink
                prefix, suffix = opener + "\n", _CLOSE_FENCE
        head = rest[:cut].strip()
        if head:
            chunks.append(head + suffix)
        rest = prefix + rest[cut:].strip()
    if rest.strip():
        chunks.append(rest.strip())
    return chunks


def _split_point(text: str, limit: int) -> int:
    """Index where the next chunk ends. Legacy order, but outside-fence breaks win."""
    half = limit // 2
    legacy = None  # the break legacy splitting would pick, even inside a fence
    for sep in ("\n\n", " "):
        p = text.rfind(sep, half, limit)
        while p != -1:
            if not _in_fence(text, p):
                return p
            if legacy is None:
                legacy = p
            p = text.rfind(sep, half, p + len(sep) - 1)
    return legacy if legacy is not None else limit


def _in_fence(text: str, pos: int) -> bool:
    """True if `pos` is inside an unclosed-so-far ``` block."""
    return text.count(_FENCE, 0, pos) % 2 == 1


def _fence_opener(text: str, pos: int) -> str:
    """The opening ``` line of the block that contains `pos`, e.g. "```python"."""
    open_idx = text.rfind(_FENCE, 0, pos)
    end = text.find("\n", open_idx)
    line = text[open_idx: end if end != -1 else len(text)]
    return line.strip()


def strip_address(text: str, names: list[str]) -> str:
    """Remove <@id> pings and bot names (whole words, any case); tidy the spacing."""
    text = re.sub(r"<@[0-9A-Za-z]+>", "", text)
    for name in sorted({n.strip() for n in names if n.strip()}, key=len, reverse=True):
        text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
