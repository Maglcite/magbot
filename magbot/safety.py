"""Dangerous-request filter for non-admin users. Defence in depth: the model also refuses."""
from __future__ import annotations

import re

DANGEROUS_PATTERNS = [
    # Legacy list, kept as it was.
    r"\brm\s+-rf\s+/\b",
    r"\brm\s+-rf\s+\S*\*",          # wildcard deletes ("rm -rf build*")
    r"mkfs\.",
    r"\bdd\s+if=/dev",
    r"\bshutdown\b",  # blocks the word anywhere, as the legacy bot did
    r"\bpx\.ok\b.*\bunlink\b",

    # Fork bomb, e.g. :(){ :|:& };:
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",
    # Recursive delete of root or home, e.g. "rm -rf ~", "rm -rf /", "rm -rf $HOME/"
    # (the trailing (?!\S) keeps "rm -rf ~/projects" and "rm -rf ./build" allowed).
    r"\brm\s+-[a-z]*r[a-z]*\s+(?:/\*?|~|\$HOME|\$\{HOME\})/?(?!\S)",
    r"\brm\b[^\n]*--no-preserve-root",
    # Making the whole filesystem world-writable.
    r"\bchmod\s+-R\s+0?777\s+/\*?(?!\S)",
    # Writing straight to a disk device: "> /dev/sda", "tee /dev/nvme0n1".
    r"(?:>|\btee\s+(?:-\S+\s+)*)\s*/dev/(?:sd|hd|vd|xvd|nvme)",
    # dd with a device as output, and mkfs with a device or option argument.
    r"\bdd\s+[^\n]*\bof=/dev/",
    r"\bmkfs(?:\.|\s+(?:-|/dev/))",
    # Power commands in command position ("sudo reboot", "x; poweroff", "init 0").
    # Plain prose like "reboot my router" is not matched.
    r"(?:[;&|]|\bsudo\b)\s*(?:reboot|poweroff|halt|init\s+[06])\b",
    # Truncating system files: ":> /etc/passwd".
    r":>\s*/etc/",
]

_DANGEROUS_REGEX = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)


def is_safe(text: str) -> bool:
    """False if `text` matches a dangerous pattern (case-insensitive)."""
    return not _DANGEROUS_REGEX.search(text)
