"""Opt-in consent: who magbot may collect data from, and the consent commands.

Policy (legacy, operator directive 2026-09-24):
  * Data is collected only from users on the opt-in allowlist (`optin.list`).
    Everyone who had used magbot before the switch was grandfathered onto it.
  * Exclusions (`optout.list`, consent.always_exclude) always win and are silent:
    an opted-out user is never prompted to opt back in.
  * consent.always_allow counts as opted in without a list entry.

Files live in config.data_path and are read live on every check, so
`magbot opt in` / `magbot opt out` apply at once. Formats are the legacy ones:
  optin.list / optout.list  one entry per line, `ID <id>` or `TAG <name#1234>`;
                            a bare entry is an id if longer than 12 chars, else a tag;
                            `#` starts a comment line.
  optin-prompted.json       {user_id: unix time of the last opt-in prompt}
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from .config import Config
from .identity import User

log = logging.getLogger(__name__)

OPTIN_FILE = "optin.list"
OPTOUT_FILE = "optout.list"
PROMPT_FILE = "optin-prompted.json"

PRESET_OPTIN_MESSAGE = (
    "🔒 hi hi~ i'd love to chat, but i need you to **opt in** first >w<\n\n"
    "my replies come from an AI model, so answering you means sending your message "
    "(and the few messages around it) to that model — and i'm not allowed to do that "
    "unless you say it's ok.\n"
    "to opt in, just say `{name} opt in` ✨\n"
    "not interested? ignore this and i'll stay quiet — and you can say "
    "`{name} opt out` any time to undo it 💛"
)


def optin_message(config: Config) -> str:
    """The opt-in prompt, addressed with the bot's first configured name."""
    return PRESET_OPTIN_MESSAGE.format(name=(config.bot.names or ["magbot"])[0])


# ── Consent commands (STRICT) ────────────────────────────────────────────────
# The only accepted form names the bot and asks directly: "magbot opt in" or
# "magbot opt out" (any configured name, or an @-ping). In a DM the phrase
# alone counts, since there the message can only be aimed at the bot.
# Bare "opt out", "!optout" and inferred privacy statements are NOT commands.
_CMD_IN_RE = re.compile(r"\bopt(?:ing)?[-\s]*(?:me[-\s]*)?(?:in|into)\b", re.I)
_CMD_OUT_RE = re.compile(r"\bopt(?:ing)?[-\s]*(?:me[-\s]*)?(?:out|off)\b", re.I)
_CMD_WINDOW = 40  # chars after the name in which the phrase must appear

# "…don't want to opt out", "no need to opt me out", "won't opt out", "never opt out"
_NEG_BEFORE_RE = re.compile(
    r"(?:don'?t|do\s+not|doesn'?t|won'?t|will\s+not|wouldn'?t|shouldn'?t|cannot|can'?t|"
    r"never|not\s+going\s+to|no\s+need\s+to|not\s+really|\bnot)\s+(?:\w+\s+){0,2}$",
    re.I,
)

# A phrase inside an advice question ("magbot, should we opt out of that?") is not a command.
_QUESTION_BEFORE_RE = re.compile(
    r"\b(?:should|should\s+we|should\s+people|should\s+i|do\s+i|do\s+we|does|why|"
    r"how|is\s+it|are\s+we|what\s+about)\b",
    re.I,
)


@functools.lru_cache(maxsize=32)
def _name_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    """Regex for any bot name, as whole words. Whitespace inside a name is flexible
    ("mag bot" also matches "magbot"); other characters are literal."""
    alts = []
    for name in sorted(set(names), key=lambda n: (-len(n), n)):
        pieces = name.split()
        if pieces:
            alts.append(r"\s*".join(re.escape(p) for p in pieces))
    if not alts:
        return re.compile(r"(?!)")  # no names configured: match nothing
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.I)


def _unguarded(text: str, start: int) -> bool:
    """True when the phrase starting at `start` is neither negated nor a question."""
    pre = text[max(0, start - 34):start]
    if _NEG_BEFORE_RE.search(pre):
        return False
    if "?" in text and _QUESTION_BEFORE_RE.search(text[max(0, start - 24):start]):
        return False
    return True


def _strict_request(text: str, name_re: re.Pattern[str], phrase_re: re.Pattern[str],
                    pinged: bool, is_dm: bool) -> bool:
    """Name-then-phrase, or (ping / DM) + phrase."""
    if not text:
        return False
    for m in name_re.finditer(text):
        pm = phrase_re.search(text, m.end(), min(len(text), m.end() + _CMD_WINDOW))
        if pm and _unguarded(text, pm.start()):
            return True
    if pinged or is_dm:
        for pm in phrase_re.finditer(text):
            if _unguarded(text, pm.start()):
                return True
    return False


def parse_command(text: str, names: list[str], *, pinged: bool, is_dm: bool) -> str | None:
    """Return "in", "out" or None for a consent command in `text`.

    `names` are the bot's configured names (config.bot.names). An opt-in phrase
    is checked before an opt-out phrase, as in the legacy bot.
    """
    text = text or ""
    name_re = _name_pattern(tuple(names))
    if _strict_request(text, name_re, _CMD_IN_RE, pinged, is_dm):
        return "in"
    if _strict_request(text, name_re, _CMD_OUT_RE, pinged, is_dm):
        return "out"
    return None


# ── Lists ────────────────────────────────────────────────────────────────────

def _parse_entries(lines: Iterable[str]) -> tuple[set[str], set[str]]:
    """Split list lines (or config entries) into (ids, tags)."""
    ids: set[str] = set()
    tags: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        up = line.upper()
        if up.startswith("ID "):
            ids.add(line.split(None, 1)[1].strip())
        elif up.startswith("TAG "):
            tags.add(line.split(None, 1)[1].strip())
        else:
            (ids if len(line) > 12 else tags).add(line)
    return ids, tags


def _matches(user: User, ids: set[str], tags: set[str]) -> bool:
    """Legacy identity match: account id, exact tag, bare name, or case-insensitive tag.

    A renamed user keeps their consent because the id matches; a tag match
    works with the old tag too.
    """
    if user.id and user.id in ids:
        return True
    if not user.tag:
        return False
    low = user.tag.lower()
    return user.bare in tags or any(low == t.lower() for t in tags)


def _refers_to(value: str, user: User) -> bool:
    """True if a list line's value is this user, for removal when toggling consent.

    Exact id, exact tag, or the tag's bare name. A tag given without a
    discriminator also drops stale `name#disc` variants. An exact `Name#1234`
    never touches a different `Name#5678`.
    """
    if user.id and value == user.id:
        return True
    tag = user.tag or ""
    if not tag:
        return False
    if value == tag or value == user.bare:
        return True
    return "#" not in tag and value.split("#", 1)[0].lower() == user.bare.lower()


def _atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text` via a temp file and os.replace (creates the directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Consent:
    """The opt-in store. Reads and rewrites the list files under config.data_path."""

    def __init__(self, config: Config):
        self._config = config
        self._optin = config.data_path / OPTIN_FILE
        self._optout = config.data_path / OPTOUT_FILE
        self._prompts = config.data_path / PROMPT_FILE
        self._lock = threading.RLock()

    # reading

    def _read(self, path: Path, *, strict: bool = False) -> list[str]:
        """Lines of a list file. Missing file -> []. Other errors are logged and
        read as empty, unless `strict` (used before rewriting, so we never clobber)."""
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            if strict:
                raise
            log.warning("consent: could not read %s: %s", path, e)
            return []

    def _optout_sets(self) -> tuple[set[str], set[str]]:
        """optout.list plus consent.always_exclude."""
        ids, tags = _parse_entries(self._read(self._optout))
        c_ids, c_tags = _parse_entries(self._config.consent.always_exclude)
        return ids | c_ids, tags | c_tags

    def is_excluded(self, user: User) -> bool:
        """Hard-excluded (opted out or operator-excluded). Always silent."""
        ids, tags = self._optout_sets()
        return _matches(user, ids, tags)

    def is_allowed(self, user: User) -> bool:
        """May this user's messages be collected and answered?"""
        if user.id and user.id == self._config.self_id:
            return True  # the bot's own messages are its own output
        if self.is_excluded(user):
            return False
        always_ids, always_tags = _parse_entries(self._config.consent.always_allow)
        if _matches(user, always_ids, always_tags):
            return True
        ids, tags = _parse_entries(self._read(self._optin))
        if _matches(user, ids, tags):
            return True
        # Legacy quirk kept: a long id written as `TAG <id>` also grants consent.
        return bool(user.id) and user.id in {t for t in tags if len(t) > 12}

    def counts(self) -> tuple[int, int, int]:
        """(allowed ids, allowed tags, excluded) for the boot log.

        Allowed includes consent.always_allow; excluded includes always_exclude.
        """
        ids, tags = _parse_entries(self._read(self._optin))
        a_ids, a_tags = _parse_entries(self._config.consent.always_allow)
        x_ids, x_tags = self._optout_sets()
        return len(ids | a_ids), len(tags | a_tags), len(x_ids) + len(x_tags)

    # toggling

    def allow(self, user: User) -> bool:
        """Opt a user in. Lifts any opt-out. True if newly added to optin.list."""
        with self._lock:
            ids, tags = _parse_entries(self._read(self._optin, strict=True))
            already = _matches(user, ids, tags)
            self._drop(self._optout, user)
            if already:
                return False
            self._append(self._optin, self._entries_for(user))
        log.info("consent: opted in %s (%s)", user.tag, user.id)
        return True

    def deny(self, user: User) -> bool:
        """Opt a user out: hard-excluded and removed from optin.list.

        True if newly excluded.
        """
        with self._lock:
            ids, tags = self._optout_sets()
            already = _matches(user, ids, tags)
            self._drop(self._optin, user)
            if already:
                return False
            self._append(self._optout, self._entries_for(user))
        log.info("consent: opted out %s (%s)", user.tag, user.id)
        return True

    @staticmethod
    def _entries_for(user: User) -> list[str]:
        entries = []
        if user.id:
            entries.append(f"ID {user.id}")
        if user.tag and user.tag != user.id:
            entries.append(f"TAG {user.tag}")
        return entries

    def _append(self, path: Path, entries: list[str]) -> None:
        if not entries:
            return
        lines = self._read(path, strict=True)
        _atomic_write(path, "".join(f"{line}\n" for line in lines + entries))

    def _drop(self, path: Path, user: User) -> None:
        """Remove this user's lines from a list file, keeping comments and blanks."""
        lines = self._read(path, strict=True)
        keep = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                keep.append(raw)
                continue
            value = line.split(None, 1)[1].strip() if " " in line else line
            if not _refers_to(value, user):
                keep.append(raw)
        if len(keep) != len(lines):
            _atomic_write(path, "".join(f"{line}\n" for line in keep))

    # opt-in prompts

    def _load_prompts(self) -> dict:
        try:
            data = json.loads(self._prompts.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("consent: could not read %s: %s", self._prompts, e)
            return {}
        return data if isinstance(data, dict) else {}

    def should_prompt(self, user: User) -> bool:
        """True if this user may be sent the opt-in prompt (per-user cooldown)."""
        if not user.id:
            return False
        try:
            last = float(self._load_prompts().get(user.id, 0))
        except (TypeError, ValueError):
            last = 0.0
        return time.time() - last >= self._config.consent.prompt_cooldown

    def mark_prompted(self, user: User) -> None:
        """Record that the opt-in prompt was just sent to this user."""
        if not user.id:
            return
        with self._lock:
            state = self._load_prompts()
            state[user.id] = int(time.time())
            try:
                _atomic_write(self._prompts, json.dumps(state))
            except OSError as e:
                log.warning("consent: could not save prompt state: %s", e)
