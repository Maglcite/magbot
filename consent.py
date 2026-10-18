"""Opt-in consent layer for magbot (the barrier between the Stoat transport and
the Hermes harness).

Policy (operator directive, 2026-09-24):
  * Data is collected ONLY from users on the allowlist (`optin.list`).
  * Every user who had already used magbot before the switch is grandfathered
    onto that list.
  * A message from anyone NOT on the list is NOT relayed to the harness, NOT
    stored as context, and NOT answered. When such a user addresses magbot by
    name (@mention or a BOT_NAMES variant) they get PRESET_OPTIN_MESSAGE once,
    explaining how to opt in.
  * Explicit exclusions (`optout.list`, `STOAT_NO_CONTEXT*` env) always win and
    are silent: an opted-out user is never prompted to opt back in.

Both sides of the wire import this module:
  * bot.py (transport)  -> gate in on_message + allowlist filter in context
  * fembot_hermes_bridge.py (harness boundary) -> defense-in-depth gate
Files are read live on every check so `opt in` / `opt out` apply immediately.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

SELF_ID = os.environ.get("STOAT_SELF_ID", "")   # the bot account's own Stoat user id
OPT_IN_FILE = os.environ.get("OPT_IN_FILE", "/opt/stoat-bot/optin.list")
OPT_OUT_FILE = os.environ.get("OPT_OUT_FILE", "/opt/stoat-bot/optout.list")
PROMPT_STATE = os.environ.get("OPTIN_PROMPT_STATE", "/opt/stoat-bot/optin-prompted.json")
PROMPT_COOLDOWN = int(os.environ.get("OPTIN_PROMPT_COOLDOWN", "43200"))  # 12h

# Operators can add identities out-of-band without editing the list file.
ENV_ALLOW_TAGS = {s.strip().lower() for s in os.environ.get("STOAT_OPTIN", "").split(",") if s.strip()}
ENV_ALLOW_IDS = {s.strip() for s in os.environ.get("STOAT_OPTIN_IDS", "").split(",") if s.strip()}

_lock = threading.Lock()

PRESET_OPTIN_MESSAGE = (
    "🔒 hi hi~ i'd love to chat, but i need you to **opt in** first >w<\n\n"
    "my replies come from an AI model, so answering you means sending your message "
    "(and the few messages around it) to that model — and i'm not allowed to do that "
    "unless you say it's ok.\n"
    "to opt in, just say `magbot opt in` ✨\n"
    "not interested? ignore this and i'll stay quiet — and you can say "
    "`magbot opt out` any time to undo it 💛"
)

# ── Consent commands (STRICT — operator directive 2026-09-24) ────────────────
# The ONLY accepted form names magbot and asks directly:
#     "magbot opt in"   /   "magbot opt out"      (also "mag bot", "mag-bot",
#     "fembot" — its display name in some servers — or an @-ping)
# The phrase must sit within 40 chars *after* the name, so "magbot is great,
# anyway i opt out of that newsletter" is not a command. In a DM/GC with magbot
# (where a message can only be aimed at it) the phrase alone counts.
# Deliberately NOT accepted: bare `opt out`, `!optout`, and inferred privacy
# statements — no way for channel chatter to flip anyone's consent.
_CMD_NAME_RE = re.compile(r"\b(?:mag\s*bot|mag-bot|fembot)\b", re.I)
_CMD_IN_RE = re.compile(r"\bopt(?:ing)?[-\s]*(?:me[-\s]*)?(?:in|into)\b", re.I)
_CMD_OUT_RE = re.compile(r"\bopt(?:ing)?[-\s]*(?:me[-\s]*)?(?:out|off)\b", re.I)
_CMD_WINDOW = 40  # chars after the name in which the phrase must appear

# "…don't want to opt out", "no need to opt me out", "won't opt out", "never opt out"
_NEG_BEFORE_RE = re.compile(
    r"(?:don'?t|do\s+not|doesn'?t|won'?t|will\s+not|wouldn'?t|shouldn'?t|cannot|can'?t|"
    r"never|not\s+going\s+to|no\s+need\s+to|not\s+really|\bnot)\s+(?:\w+\s+){0,2}$",
    re.I,
)

# A phrase inside an advice question ("magbot, should we opt out of that?") is
# not a command.
_QUESTION_BEFORE_RE = re.compile(
    r"\b(?:should|should\s+we|should\s+people|should\s+i|do\s+i|do\s+we|does|why|"
    r"how|is\s+it|are\s+we|what\s+about)\b",
    re.I,
)


def _unguarded(text: str, start: int) -> bool:
    """True when the phrase starting at `start` isn't negated or a question."""
    pre = text[max(0, start - 34):start]
    if _NEG_BEFORE_RE.search(pre):
        return False
    if "?" in text and _QUESTION_BEFORE_RE.search(text[max(0, start - 24):start]):
        return False
    return True


def _strict_request(text: str, pattern: re.Pattern, pinged: bool, is_dm: bool) -> bool:
    """Name-then-phrase (or ping/DM + phrase) matcher."""
    if not text:
        return False
    for m in _CMD_NAME_RE.finditer(text):
        pm = pattern.search(text, m.end(), min(len(text), m.end() + _CMD_WINDOW))
        if pm and _unguarded(text, pm.start()):
            return True
    if pinged or is_dm:
        for pm in pattern.finditer(text):
            if _unguarded(text, pm.start()):
                return True
    return False


def _read_list(path: str):
    """Return (ids, tags) from a list file (format: `ID <id>` / `TAG <tag>`)."""
    ids, tags = set(), set()
    try:
        with open(path) as f:
            for raw in f:
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
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return ids, tags


def _read_opted_out():
    """optout.list plus the legacy env blocks (always denied)."""
    ids, tags = _read_list(OPT_OUT_FILE)
    ids |= {s.strip() for s in os.environ.get("STOAT_NO_CONTEXT_IDS", "").split(",") if s.strip()}
    tags |= {s.strip() for s in os.environ.get("STOAT_NO_CONTEXT", "").split(",") if s.strip()}
    return ids, tags


def _identity_matches(user_id, tag, ids, tags) -> bool:
    if user_id and user_id in ids:
        return True
    if not tag:
        return False
    bare = tag.split("#", 1)[0]
    low = tag.lower()
    for t in tags:
        if tag == t or bare == t or low == t.lower():
            return True
    return False


def is_excluded(user_id, tag) -> bool:
    """Hard-denied: opted out / operator-excluded. Always silent."""
    ids, tags = _read_opted_out()
    return _identity_matches(user_id, tag, ids, tags)


def is_allowed(user_id, tag) -> bool:
    """May this author's messages be collected (relayed to the harness)?"""
    if user_id and user_id == SELF_ID:
        return True  # magbot's own messages are its own output
    if is_excluded(user_id, tag):
        return False
    if user_id and user_id in ENV_ALLOW_IDS:
        return True
    if tag and tag.lower() in ENV_ALLOW_TAGS:
        return True
    ids, tags = _read_list(OPT_IN_FILE)
    if _identity_matches(user_id, tag, ids, tags):
        return True
    # The bridge only has the tag; also honour ids that appear as bare tags.
    if user_id and user_id in {t for t in tags if len(t) > 12}:
        return True
    return False


def _write_append(path: str, entries) -> None:
    with _lock:
        with open(path, "a") as f:
            for e in entries:
                f.write(e + "\n")


def _remove_from_file(path: str, user_id, tag) -> None:
    """Strip any lines matching this identity (used when toggling consent)."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    bare = (tag or "").split("#", 1)[0].lower()
    keep = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            keep.append(raw)
            continue
        val = line.split(None, 1)[1].strip() if " " in line else line
        vbare = val.split("#", 1)[0].lower()
        # Match: exact id, exact tag, or the tag's bare name. When we were given
        # a tag without a discriminator we also drop stale `name#disc` variants
        # of it (renamed/newly-registered accounts). An exact `Name#1234` never
        # touches a different `Name#5678`.
        if ((user_id and val == user_id)
                or (tag and (val == tag or val == bare))
                or (bare and "#" not in tag and vbare == bare)):
            continue
        keep.append(raw)
    with _lock:
        with open(path, "w") as f:
            f.writelines(keep)


def allow(user_id, tag) -> bool:
    """Opt a user in (idempotent). Lifts any prior opt-out. Returns True if newly added."""
    ids, tags = _read_list(OPT_IN_FILE)
    already = _identity_matches(user_id, tag, ids, tags)
    _remove_from_file(OPT_OUT_FILE, user_id, tag)
    if already:
        return False
    entries = []
    if user_id:
        entries.append(f"ID {user_id}")
    if tag:
        entries.append(f"TAG {tag}")
    if entries:
        _write_append(OPT_IN_FILE, entries)
    return True


def deny(user_id, tag) -> bool:
    """Opt a user out: hard-blocked and removed from the allowlist."""
    ids, tags = _read_opted_out()
    already = _identity_matches(user_id, tag, ids, tags)
    _remove_from_file(OPT_IN_FILE, user_id, tag)
    if already:
        return False
    entries = []
    if user_id:
        entries.append(f"ID {user_id}")
    if tag:
        entries.append(f"TAG {tag}")
    if entries:
        _write_append(OPT_OUT_FILE, entries)
    return True


def _load_prompt_state():
    try:
        with open(PROMPT_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def should_prompt(user_id) -> bool:
    """True if we may send this user the preset opt-in message (cooldown)."""
    if not user_id:
        return False
    last = _load_prompt_state().get(user_id, 0)
    try:
        last = float(last)
    except Exception:
        last = 0
    return (time.time() - last) >= PROMPT_COOLDOWN


def mark_prompted(user_id) -> None:
    if not user_id:
        return
    with _lock:
        state = _load_prompt_state()
        state[user_id] = int(time.time())
        tmp = PROMPT_STATE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, PROMPT_STATE)
        except Exception:
            pass


def optin_requested(text: str, pinged: bool = False, is_dm: bool = False) -> bool:
    """True only for an explicit "magbot opt in" (or @-ping / DM + "opt in")."""
    return _strict_request(text or "", _CMD_IN_RE, pinged, is_dm)


def optout_requested(text: str, pinged: bool = False, is_dm: bool = False) -> bool:
    """True only for an explicit "magbot opt out" (or @-ping / DM + "opt out")."""
    return _strict_request(text or "", _CMD_OUT_RE, pinged, is_dm)
