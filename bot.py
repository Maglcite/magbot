#!/usr/bin/env python3
"""
Stoat.chat bot — ping/name-triggered AI assistant.

BACKEND: Hermes Agent via a persistent ACP bridge (the "fembot" profile).
This file is the Stoat *transport* layer: it handles ping/name detection,
moderation commands, image attachments, safety gating, and reply splitting.
All actual intelligence, memory, and tools (web search, browse, shell, file
hosting, image gen) are provided by Hermes Agent running as a persistent ACP
session in the dedicated 'fembot' profile (see fembot_hermes_bridge.py).

- Responds when pinged (any channel) OR when name-mentioned in any channel
- Uses the fembot Hermes profile (persona preserved from the old brain)
- Anyone in the server can use it
- Safety: the request is filtered before reaching Hermes; Hermes itself refuses
  destructive/malicious actions.
"""

import os
import re
import sys
import json
import time
import queue
import asyncio
import logging
import subprocess
import threading

import stoat

# Opt-in consent layer (allowlist). Shared with the harness bridge so both
# sides of the wire enforce the same policy.
import consent

log = logging.getLogger("stoat_bot")

# ─── Configuration ───────────────────────────────────────────────────────────

STOAT_TOKEN = os.environ.get("STOAT_BOT_TOKEN", "")
HERMES_PY = os.environ.get(
    "HERMES_PY", "/usr/local/lib/hermes-agent/venv/bin/python"
)
BRIDGE_SCRIPT = os.environ.get(
    "BRIDGE_SCRIPT", "/opt/stoat-bot/fembot_hermes_bridge.py"
)
BRIDGE_TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "120"))

PROFILES_FILE = "/opt/stoat-bot/profiles.md"   # kept for reference/audit only
WWW_DIR = "/opt/stoat-bot/www"
# Public base URL that WWW_DIR is served under (e.g. via a reverse proxy/tunnel)
FILE_BASE_URL = os.environ.get("FILE_BASE_URL", "https://example.com").rstrip("/")
BOT_NAMES = {s.strip().lower() for s in os.environ.get("BOT_NAMES", "magbot,mag bot,mag-bot").split(",") if s.strip()}   # name variants to detect in text

# How many recent messages before the trigger to read as conversational context
# and feed to Hermes alongside the caller's message (0 disables the feature).
RECENT_CONTEXT_LIMIT = int(os.environ.get("RECENT_CONTEXT_LIMIT", "8"))

# ─── Thinking traces ─────────────────────────────────────────────────────────
# The Hermes brain streams its reasoning (ACP agent_thought_chunk) to the bridge,
# which hands it back with each reply. Mirror what Hermes does on Discord (a
# small "💭 Reasoning" block above the answer), adapted to Stoat's markdown:
# Stoat has no Discord subtext ("-#"), so a blockquote is the closest primitive.
#   THINKING_MODE = blockquote | spoiler | plain | off
#     off        -> never show traces  (DEFAULT — operator directive 2026-09-24:
#                   no visible thinking tokens in chat; set THINKING_MODE to
#                   blockquote/spoiler/plain in the unit to bring them back)
#     blockquote -> "> 💭 **Thinking**" + quoted lines
#     spoiler    -> the same content wrapped in ||spoiler|| (click to reveal)
#     plain      -> header + raw thought text, no quoting
THINKING_MODE = os.environ.get("THINKING_MODE", "off").strip().lower()
THINKING_MAX_LINES = int(os.environ.get("THINKING_MAX_LINES", "15"))
THINKING_MAX_CHARS = int(os.environ.get("THINKING_MAX_CHARS", "1200"))

# Watchdog: how often (seconds) the bot probes the bridge so a wedged ACP
# session self-heals even when nobody is chatting. The probe is a real (tiny)
# model call, so tune to your cost tolerance. 0 disables the watchdog.
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "300"))
HEARTBEAT_PROMPT = os.environ.get(
    "HEARTBEAT_PROMPT",
    "Automated liveness check. Reply with exactly one word: ok",
)

# Admin accounts bypass the safety filter entirely and are unrestricted in
# what the underlying agent may be asked to do.
# Comma-separated exact "name#1234" tags, from STOAT_ADMINS.
ADMIN_ACCOUNTS = {s.strip() for s in os.environ.get("STOAT_ADMINS", "").split(",") if s.strip()}

# ─── Purge: delete one user's messages in a single channel ───────────────────
# Two Stoat rules shape this:
#   * the bulk endpoint takes at most 100 ids per request and refuses any
#     message that is >= 1 week old, so older ones go one request at a time;
#   * both paths need Manage Messages in that channel, so the bot's role needs it.
# Always a dry run first: `/purge @user` only counts, `/purge @user confirm` deletes.
PURGE_SCAN_LIMIT = int(os.environ.get("PURGE_SCAN_LIMIT", "2000"))   # messages of history we look back through
PURGE_CONFIRM_TTL = int(os.environ.get("PURGE_CONFIRM_TTL", "300"))  # seconds a preview waits for a "confirm"
PURGE_BULK_MAX = 100                        # Stoat's cap on ids per bulk-delete request
PURGE_BULK_MAX_AGE = 7 * 24 * 60 * 60      # the bulk endpoint rejects anything older
PURGE_PENDING: dict = {}                    # (channel_id, target_id) -> {"by", "expires", "ids"}
PURGE_PACE = float(os.environ.get("PURGE_PACE", "0.35"))   # seconds between delete calls (rate-limit courtesy)
PURGE_MAX_DELETE = int(os.environ.get("PURGE_MAX_DELETE", "500"))  # per-confirm ceiling; raise it deliberately
# Stoat's slash UI can swallow leading-slash commands (it ate `/ban` the same
# way), so the word-form aliases do the same job when the slash command never
# arrives. `/del-all` is the headline name; the older spellings keep working.
PURGE_ALIASES = ("/del-all", "/delall", "/del_all", "/purge",
                 "delalldelalldelall", "purgepurgepurge")

# Consent filter: messages from these accounts are NEVER forwarded to the
# Hermes bridge, so they are never taken as context by the AI at all.
# Format: comma-separated exact "name#1234" tags and/or raw Stoat user IDs.
# Populated from the STOAT_NO_CONTEXT / STOAT_NO_CONTEXT_IDS env vars
# (set in the stoat-bot systemd unit).
NO_CONTEXT_TAGS = {s.strip() for s in os.environ.get("STOAT_NO_CONTEXT", "").split(",") if s.strip()}
NO_CONTEXT_IDS = {s.strip() for s in os.environ.get("STOAT_NO_CONTEXT_IDS", "").split(",") if s.strip()}

# Persistent opt-out list. Users who ask magbot to exclude them from AI context
# are written here so the exclusion survives restarts. One entry per line, either:
#   ID <stoat user id>      (preferred, stable)
#   TAG <name#1234>
# Loaded live on each check (no restart needed when someone opts out).
OPT_OUT_FILE = os.environ.get("OPT_OUT_FILE", "/opt/stoat-bot/optout.list")
_opt_lock = threading.Lock()

def _load_opt_out():
    """Return (ids, tags) read from the persistent opt-out file (live)."""
    ids, tags = set(), set()
    try:
        with open(OPT_OUT_FILE) as f:
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
                    # bare entry: treat as ID (stable) if long, else tag
                    (ids if len(line) > 12 else tags).add(line)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug(f"opt-out list read error: {e}")
    return ids, tags

def _is_consent_excluded(message):
    """True if this message's author opted out of being used as AI context."""
    _aid, _atag = _author_identity(message)
    _abare = _atag.split("#", 1)[0]
    o_ids, o_tags = _load_opt_out()
    return (
        _aid in NO_CONTEXT_IDS or _aid in o_ids
        or _atag in NO_CONTEXT_TAGS or _atag in o_tags
        or _abare in NO_CONTEXT_TAGS or _abare in o_tags
    )

def _add_opt_out(aid, atag):
    """Persist an opt-out for a user (idempotent). Returns False if already excluded."""
    o_ids, o_tags = _load_opt_out()
    if aid in o_ids or atag in o_tags:
        return False
    with _opt_lock:
        try:
            with open(OPT_OUT_FILE, "a") as f:
                f.write(f"ID {aid}\nTAG {atag}\n")
        except Exception as e:
            log.error(f"opt-out write failed: {e}")
            return False
    log.info("Added %s (%s) to opt-out list", atag, aid)
    return True

# Opt-out request detection (matched on the raw message text).
_OPTOUT_RE = re.compile(
    r"\b(?:opt\s*me?\s*(?:out|of(?:f)?)|opt-out|optout|exclud(?:e|ing)\s*me|"
    r"don\'?t\s+use\s+my\s+(?:messages|data|context)|stop\s+(?:reading|using)\s+my\s+"
    r"(?:messages|data)|remove\s+me\s+from\s+(?:your\s+)?(?:memory|context|records)|"
    r"privacy\s*opt(?:-)?out|i\s+don\'?t\s+want\s+(?:my\s+)?(?:messages|data)\s+(?:read|used|collected|processed))\b",
    re.IGNORECASE,
)

def _author_identity(message):
    """Return (author_id, author_tag) for a message.

    Defensive: historical messages in channel history may not have their author
    cached, in which case the `author` property raises stoat NoData. Fall back
    to the always-present author_id so context building never crashes the bot.
    """
    _id = str(getattr(message, "author_id", "") or "")
    _au = None
    try:
        _au = getattr(message, "author", None)
    except Exception:
        _au = None
    _name = (getattr(_au, "name", None) or getattr(_au, "display_name", None) or _id) if _au is not None else _id
    _disc = getattr(_au, "discriminator", None)
    _tag = f"{_name}#{_disc}" if _disc else _name
    return _id, _tag


async def _recent_context(message) -> str:
    """Read the last RECENT_CONTEXT_LIMIT messages before `message` in its
    channel and render them as a [PREVIOUS MESSAGES] block for Hermes.

    Respects the consent filter (excluded authors' text is omitted), strips
    ping mentions of the bot itself, and stops at the caller's message so the
    agent sees the thread that led up to the mention.
    """
    if RECENT_CONTEXT_LIMIT <= 0:
        return ""
    channel = getattr(message, 'channel', None)
    if channel is None or not hasattr(channel, 'history'):
        return ""
    try:
        before_id = getattr(message, 'id', None)
        msgs = await channel.history(limit=RECENT_CONTEXT_LIMIT, before=before_id)
    except Exception as e:
        log.debug(f"recent-context fetch failed: {e}")
        return ""
    if not msgs:
        return ""
    lines = []
    _ctx_skipped = 0
    for m in msgs:
        try:
            # Opt-in allowlist: only messages from users who consented (plus
            # magbot's own) may become context. Everyone else is invisible here
            # — their text is never read into a prompt.
            _mid, _mtag = _author_identity(m)
            if not consent.is_allowed(_mid, _mtag):
                _ctx_skipped += 1
                continue
            t = (m.content or "").strip()
            if not t:
                continue
            # Remove mentions of the bot itself so the stacked pings don't clutter.
            t = re.sub(r"<@[0-9A-Za-z]+>", "", t).strip()
            t = re.sub(r"\b(?:magbot|mag bot|mag-bot)\b", "", t, flags=re.IGNORECASE).strip()
            if not t:
                continue
            _, tag = _author_identity(m)
            lines.append(f"- {tag}: {t[:400]}")
        except Exception as e:
            log.debug(f"recent-context skipped a message: {e}")
            continue
    if _ctx_skipped:
        log.info("context: omitted %d message(s) from authors without opt-in", _ctx_skipped)
    if not lines:
        return ""
    return "[PREVIOUS MESSAGES IN THIS CHANNEL]\n" + "\n".join(lines) + "\n"

# Trusted non-admin users allowed to make the agent run non-destructive shell
# work. (Hermes itself still guards destructive commands.)
# Comma-separated exact "name#1234" tags, from STOAT_SHELL_USERS.
SHELL_USERS = {s.strip() for s in os.environ.get("STOAT_SHELL_USERS", "").split(",") if s.strip()}

# Per-user personality overrides. Injected into the message sent to Hermes so
# the persona can change for a specific user only (e.g. roast repeat harassers).
# Optional JSON file mapping "name#1234" -> extra instruction text.
PERSONALITY_OVERRIDES_FILE = os.environ.get(
    "PERSONALITY_OVERRIDES_FILE", "/opt/stoat-bot/personality_overrides.json"
)
try:
    with open(PERSONALITY_OVERRIDES_FILE) as _f:
        PERSONALITY_OVERRIDES = json.load(_f)
except FileNotFoundError:
    PERSONALITY_OVERRIDES = {}

# ─── Safety filter (defense-in-depth; the agent also refuses on its own) ─────

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/\b",
    r"\brm\s+-rf\s+[^ ]*?\*\b",
    r"mkfs\.",
    r"\bdd\s+if=/dev",
    r"\bshutdown\b",
    r"\bpx\.ok\b.*\bunlink\b",
]
DANGEROUS_REGEX = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)

def is_safe(text: str) -> bool:
    return not bool(DANGEROUS_REGEX.search(text))


# ─── Stoat markdown sanitiser ────────────────────────────────────────────────
#
# Stoat renders GFM with `singleTilde` strikethrough enabled, so a SINGLE pair
# of tildes ("~like this~") strikes out everything between them. The fembot
# persona's cute "~" verbal tics therefore accidentally strike the first few
# words of a reply (e.g. "~o-oh! w-well~ :3 ..." -> "o-oh! w-well" struck).
# We swap tildes for the visually-identical fullwidth tilde (U+FF5E), which has
# no markdown meaning. Tildes inside `code spans` / ```code blocks``` are kept.

_FW_TILDE = "\uff5e"
_CODE_SPLIT = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def sanitize_markdown(text: str) -> str:
    if "~" not in text:
        return text
    parts = _CODE_SPLIT.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:  # even index = outside code -> neutralise tildes
            parts[i] = part.replace("~", _FW_TILDE)
    return "".join(parts)


_THINK_HEADER = "💭 **Thinking**"


def format_thinking(reasoning: str) -> str:
    """Render the model's reasoning as a Stoat markdown block, or "" to hide it.

    Mirrors Hermes' Discord behaviour: only shown when the model actually
    reasoned, capped to THINKING_MAX_LINES lines (with an explicit "N more
    lines" note so the trace is never silently truncated).
    """
    if THINKING_MODE == "off" or not reasoning or not reasoning.strip():
        return ""
    text = sanitize_markdown(reasoning.strip())
    if len(text) > THINKING_MAX_CHARS:
        text = text[:THINKING_MAX_CHARS].rstrip() + "…"
    lines = text.splitlines()
    dropped = 0
    if THINKING_MAX_LINES > 0 and len(lines) > THINKING_MAX_LINES:
        dropped = len(lines) - THINKING_MAX_LINES
        lines = lines[:THINKING_MAX_LINES]
    body = "\n".join(lines).strip("\n")
    if THINKING_MODE == "plain":
        block = f"{_THINK_HEADER}\n{body}"
        if dropped:
            block += f"\n_… {dropped} more lines_"
        return block
    # blockquote / spoiler share the quoted body
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in body.splitlines())
    block = f"> {_THINK_HEADER}\n{quoted}"
    if dropped:
        block += f"\n> _… {dropped} more lines_"
    if THINKING_MODE == "spoiler":
        # Multi-line spoilers render fine in Stoat; the header stays visible so
        # readers know what is hidden.
        block = f"||\n{block}\n||"
    return block

# ─── Persistent Hermes ACP bridge client ─────────────────────────────────────

class HermesBridge:
    """Spawn + hold the persistent Hermes ACP bridge subprocess.

    Protocol (one JSON object per line):
      in : {"id": N, "message": "...", "user": "name#1234"}
      out: {"id": N, "ok": true, "reply": "...", "ms": 1234}
           {"id": N, "ok": false, "error": "..."}
    Messages are processed serially (the ACP session is single-turn), so a lock
    serializes requests and the id echoes the request id.

    Robustness: every request is hard-bounded by BRIDGE_TIMEOUT. A dedicated
    reader thread drains stdout into a queue so ask() can time out a hang even
    if the bridge subprocess stops responding (previously a wedged ACP session
    blocked readline() forever, holding the lock for every later call). On any
    timeout/error the bridge is bounced so the NEXT message spawns a fresh ACP
    session (self-heal). A watchdog thread probes on a schedule so a silent
    wedge is caught even when nobody is chatting.
    """

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self._seq = 0
        self._ready = False
        self._inbox = queue.Queue()          # reader thread -> ask()/ensure_ready
        self._reader = None
        self._spawn_lock = threading.Lock()  # guards respawn, not serialization
        self._stderr_tail: list = []         # last stderr lines (diagnostics only)

    def _start_reader(self, proc, inbox):
        """Drain `proc` stdout into `inbox` so bridge reads can time out.

        The proc and inbox are captured per-spawn so an old reader thread can
        never mistarget a newer process after a bounce.
        """
        def _drain():
            try:
                for line in proc.stdout:
                    inbox.put(line)
            except Exception:
                pass
            finally:
                try:
                    inbox.put(None)  # EOF sentinel
                except Exception:
                    pass
        self._reader = threading.Thread(target=_drain, daemon=True)
        self._reader.start()

    def _start_stderr_reader(self, proc):
        """Drain `proc` stderr into a bounded ring buffer.

        A stderr PIPE that nobody reads fills up after ~64 KB and then BLOCKS
        the bridge on its next write — a silent, permanent wedge (the bridge
        looks alive, but every later request hangs). Draining is mandatory;
        we keep only the last few lines for diagnostics.
        """
        tail = self._stderr_tail

        def _drain_err():
            try:
                for line in proc.stderr:
                    s = (line or "").rstrip()
                    if s:
                        tail.append(s)
                        del tail[:-40]
            except Exception:
                pass
        threading.Thread(target=_drain_err, daemon=True).start()

    def _spawn(self):
        with self._spawn_lock:
            self.proc = subprocess.Popen(
                [HERMES_PY, BRIDGE_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            log.info(f"Bridge subprocess started (pid={self.proc.pid})")
            self._inbox = queue.Queue()
            self._stderr_tail.clear()
            self._start_reader(self.proc, self._inbox)
            self._start_stderr_reader(self.proc)

    def _read_until_ready(self, timeout: float) -> bool:
        """Wait up to `timeout` for the bridge's ready event through the queue."""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            try:
                line = self._inbox.get(timeout=remaining)
            except queue.Empty:
                return self.proc.poll() is None  # still alive but silent
            if line is None:
                return False                     # EOF -> process exited
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if evt.get("event") == "ready":
                self._ready = True
                log.info(f"Bridge ready: session={evt.get('session')}")
                return True

    def ensure_ready(self):
        restart = False
        if self.proc and self.proc.poll() is None:
            if not self._ready:
                # Fresh process but no ready line yet — wait for it.
                if self._read_until_ready(30):
                    return True
                restart = True
            else:
                return True
        else:
            restart = True
        if restart:
            self._bounce(quiet=True)
            self._spawn()
            log.info("Waiting for fresh bridge to become ready...")
            ok = self._read_until_ready(45)
            if not ok:
                log.warning("Bridge failed to become ready")
                self._bounce(quiet=True)
            return ok

    def ask(self, message: str, user: str = "") -> str:
        """Return just the reply text (back-compat wrapper)."""
        return self.ask_full(message, user)[0]

    def ask_full(self, message: str, user: str = "") -> tuple:
        """Ask the brain; return (reply, reasoning).

        `reasoning` is the model's streamed thinking for this turn, or "" when it
        produced none. Same serialization/timeout semantics as before.
        """
        # Time-bounded lock acquisition so a wedged caller can't freeze the
        # whole bot behind it: wait up to BRIDGE_TIMEOUT for the lock, else error.
        if not self.lock.acquire(timeout=BRIDGE_TIMEOUT):
            self._bounce(quiet=True)
            raise TimeoutError("hermes bridge lock held too long")
        try:
            self.ensure_ready()
            if not self._ready:
                raise RuntimeError("hermes bridge not ready")
            self._seq += 1
            rid = self._seq
            req = {"id": rid, "message": message, "user": user}
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            # Read until we get OUR id, bounded by BRIDGE_TIMEOUT total.
            deadline = time.time() + BRIDGE_TIMEOUT
            resp = None
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("hermes bridge timed out")
                try:
                    line = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError("hermes bridge timed out")
                if line is None:
                    raise RuntimeError("hermes bridge subprocess exited")
                try:
                    resp = json.loads(line)
                except Exception:
                    continue
                if resp.get("id") == rid:
                    break
            if not resp.get("ok", False):
                raise RuntimeError(resp.get("error", "hermes error"))
            return resp.get("reply", ""), (resp.get("reasoning") or "")
        except Exception as e:
            # Wedge/error: bounce so the next message starts a clean session.
            self._bounce(quiet=False)
            raise
        finally:
            self.lock.release()

    def ping(self, timeout: float = 15.0) -> bool:
        """Cheap liveness probe.

        The bridge answers `{"ping": true}` itself (no ACP/model turn), so this
        costs milliseconds instead of a full round trip — essential now that the
        backend is a local model whose replies take ~3 minutes.
        """
        if not self.lock.acquire(timeout=timeout):
            raise TimeoutError("hermes bridge lock held too long")
        try:
            self.ensure_ready()
            if not self._ready:
                raise RuntimeError("hermes bridge not ready")
            self._seq += 1
            rid = self._seq
            self.proc.stdin.write(json.dumps({"id": rid, "ping": True}) + "\n")
            self.proc.stdin.flush()
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("hermes bridge ping timed out")
                try:
                    line = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError("hermes bridge ping timed out")
                if line is None:
                    raise RuntimeError("hermes bridge subprocess exited")
                try:
                    resp = json.loads(line)
                except Exception:
                    continue
                if resp.get("id") == rid:
                    return bool(resp.get("ok"))
        except Exception:
            self._bounce(quiet=False)
            raise
        finally:
            self.lock.release()

    def heartbeat(self):
        """Cheap liveness probe; never preempts an in-flight reply.

        If the lock is held, a real request is being served — that is *healthy*,
        so skip instead of bouncing (bouncing here used to kill live replies and
        start the bounce loop). Only a free lock plus a failing ping is a wedge.
        """
        if not self.lock.acquire(blocking=False):
            log.info("Watchdog heartbeat skipped (bridge busy serving a request)")
            return
        self.lock.release()
        try:
            self.ping(timeout=15.0)
            log.info("Watchdog heartbeat OK")
        except Exception as e:
            log.warning(f"Watchdog probe failed ({e}) — bridge bounced, will self-heal on next use")

    def _bounce(self, quiet: bool = False):
        """Kill a wedged/errored bridge so the next call respawns it cleanly."""
        if not quiet:
            log.warning("Bouncing hermes bridge (wedged/errored)")
            if self._stderr_tail:
                log.warning("bridge stderr (tail): " + " | ".join(self._stderr_tail[-4:]))
        p, self.proc = self.proc, None
        self._ready = False
        # Drain any stale queued lines so a fresh spawn starts clean.
        try:
            while True:
                self._inbox.get_nowait()
        except Exception:
            pass
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


bridge = HermesBridge()

# ─── Attachment download ─────────────────────────────────────────────────────

async def download_attachments(message) -> list[str]:
    """Download image attachments to the www folder (so Hermes can see them)."""
    saved = []
    try:
        for att in getattr(message, "attachments", []) or []:
            ctype = getattr(att, "content_type", "") or ""
            meta = getattr(att, "metadata", None)
            meta_type = getattr(meta, "type", None)
            is_image = "image" in ctype.lower() or (
                meta_type is not None and "image" in str(meta_type).lower()
            )
            if not is_image:
                continue
            data = await att.read()
            if not data:
                continue
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
                   "image/gif": "gif", "image/webp": "webp"}.get(ctype.lower(), "img")
            fname = f"upload_{str(att.id)[-12:] or int(time.time())}.{ext}"
            fpath = os.path.join(WWW_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(data)
            saved.append(fpath)
            log.info(f"Saved attachment {fname} ({len(data)} bytes)")
    except Exception as e:
        log.error(f"download_attachments error: {e}")
    return saved

# ─── Bot ─────────────────────────────────────────────────────────────────────

class StoatBot(stoat.Client):
    """Stoat.chat bot — ping or name-mention triggered, Hermes-backed brain."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def on_ready(self, event, /):
        log.info(f"Logged in as {event.me.tag} (ID: {event.me.id})")
        log.info("Fembot online — Hermes ACP backend. Responds to pings and name mentions.")
        # Consent layer status: the opt-in allowlist gates everything that
        # follows, so print it on every boot for auditability.
        try:
            _a_ids, _a_tags = consent._read_list(consent.OPT_IN_FILE)
            _x_ids, _x_tags = consent._read_opted_out()
            log.info("Consent: OPT-IN allowlist active — %d account ids / %d tag aliases allowed; "
                     "%d excluded (opt-out)", len(_a_ids), len(_a_tags), max(len(_x_ids), len(_x_tags)))
        except Exception as e:
            log.warning("Consent: could not summarise lists: %s", e)
        for s in (event.servers or []):
            log.info(f"  Server: {s.name} (ID: {s.id})")
        # Pre-warm the Hermes ACP bridge in the background so the first ping
        # doesn't pay the cold-spawn cost (~40s).
        def _warm():
            try:
                bridge.ensure_ready()
                log.info("Hermes bridge pre-warmed")
            except Exception as e:
                log.error(f"Hermes bridge pre-warm failed: {e}")
        threading.Thread(target=_warm, daemon=True).start()

        # Start the idle watchdog so a silently-wedged ACP session self-heals
        # even when nobody is chatting to the bot.
        def _watch():
            while True:
                try:
                    threading.Event().wait(HEARTBEAT_INTERVAL)
                    if HEARTBEAT_INTERVAL > 0:
                        bridge.heartbeat()
                except Exception as e:
                    log.error(f"Watchdog loop error: {e}")
        if HEARTBEAT_INTERVAL > 0:
            threading.Thread(target=_watch, daemon=True).start()
            log.info(f"Watchdog started (heartbeat every {HEARTBEAT_INTERVAL}s)")

    def _get_channel_name(self, message) -> str:
        try:
            channel = message.channel
            return (getattr(channel, 'name', None) or str(channel.id)).lower()
        except Exception:
            return ""

    def _is_name_mentioned(self, text: str) -> bool:
        text_lower = text.lower()
        return any(name in text_lower for name in BOT_NAMES)

    @staticmethod
    def _is_dm_like(channel) -> bool:
        """DM / GC / saved-messages channels: everything there is addressed to
        the bot, so consent commands don't need to name it."""
        t = getattr(channel, "channel_type", None) or getattr(channel, "type", None)
        t = getattr(t, "value", t)
        return str(t or "").lower() in ("directmessage", "group", "savedmessages")

    # ── Admin moderation commands ────────────────────────────────────────

    def _extract_target_user_id(self, text: str) -> str | None:
        m = re.search(r"<@([0-9A-Za-z]+)>", text)
        if m:
            return m.group(1)
        m2 = re.search(r"\b(01[0-9A-Z]{24})\b", text)
        if m2:
            return m2.group(1)
        return None

    def _extract_reason(self, text: str) -> str | None:
        stripped = re.sub(r"^/(ban|kick|unban)\s+", "", text, flags=re.IGNORECASE)
        stripped = re.sub(r"^banbanbanban\s+", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"<@[0-9A-Za-z]+>\s*", "", stripped)
        stripped = re.sub(r"\b01[0-9A-Z]{24}\b\s*", "", stripped)
        return stripped.strip() or None

    async def _handle_moderation(self, message, text: str) -> bool:
        text_stripped = text.strip()
        if not text_stripped.startswith("/") and not text_stripped.lower().startswith("banbanbanban"):
            return False
        cmd_lower = text_stripped.lower()
        is_ban = (cmd_lower.startswith("/ban ") or cmd_lower == "/ban"
                  or cmd_lower.startswith("banbanbanban ") or cmd_lower == "banbanbanban")
        is_kick = cmd_lower.startswith("/kick ") or cmd_lower == "/kick"
        is_unban = cmd_lower.startswith("/unban ") or cmd_lower == "/unban"
        if not (is_ban or is_kick or is_unban):
            return False

        try:
            _u = getattr(message.author, "name", None) or getattr(message.author, "display_name", None) or "?"
            _d = getattr(message.author, "discriminator", None)
            author_tag = f"{_u}#{_d}" if _d else _u
        except Exception:
            author_tag = str(message.author_id)

        if author_tag not in ADMIN_ACCOUNTS:
            try:
                await message.reply("🚫 Only admins can use moderation commands.")
            except Exception:
                pass
            log.warning(f"Non-admin {author_tag} attempted moderation command: {text_stripped[:80]}")
            return True

        target_id = self._extract_target_user_id(text_stripped)
        if not target_id:
            try:
                await message.reply("⚠️ Usage: `banbanbanban @user [reason]` (or `/ban @user [reason]`) — mention the user or paste their ID.")
            except Exception:
                pass
            return True
        reason = self._extract_reason(text_stripped)

        try:
            server = message.server or (message.channel and getattr(message.channel, "server", None))
        except Exception:
            server = None
        if server is None:
            try:
                await message.reply("⚠️ Can't determine the server from this message. Use this in a server channel.")
            except Exception:
                pass
            return True

        action = "ban" if is_ban else ("kick" if is_kick else "unban")
        log.info(f"Admin {author_tag} executing /{action} on user {target_id} (reason: {reason})")
        try:
            if is_ban:
                await server.ban(target_id, reason=reason)
                msg = f"🔨 Banned <@{target_id}>{f' — reason: {reason}' if reason else ''}."
            elif is_kick:
                member = await server.fetch_member(target_id)
                if member:
                    await member.kick()
                    msg = f"👢 Kicked <@{target_id}>."
                else:
                    await server.kick(target_id)
                    msg = f"👢 Kicked <@{target_id}>."
            elif is_unban:
                await server.unban(target_id)
                msg = f"✅ Unbanned <@{target_id}>."
            try:
                await message.reply(msg)
            except Exception:
                pass
            log.info(f"/{action} succeeded for {target_id} by {author_tag}")
        except Exception as e:
            err = str(e)
            log.error(f"/{action} failed for {target_id}: {err}")
            try:
                await message.reply(f"⚠️ /{action} failed: {err[:200]}")
            except Exception:
                pass
        return True

    # ── Purge command: delete one user's messages in this channel ────────

    @staticmethod
    def _ulid_time(mid) -> float:
        """Seconds-since-epoch embedded in a ULID-style id (0.0 if unparsable)."""
        s = str(mid)
        if len(s) < 10:
            return 0.0
        n = 0
        for ch in s[:10].upper():
            i = "0123456789ABCDEFGHJKMNPQRSTVWXYZ".find(ch)
            if i < 0:
                return 0.0
            n = (n << 5) | i
        return n / 1000.0

    @staticmethod
    def _purge_err_kind(err) -> str:
        """Classify a delete failure: 'fatal' (stop), 'gone' (already deleted), 'failed'."""
        blob = (str(getattr(err, "type", "") or "") + " " + str(err or "")).lower()
        if any(k in blob for k in ("missingpermission", "missing permission", "forbidden",
                                   "unauthorized", "notauthorised", "not authorised",
                                   "notauthenticated")):
            return "fatal"
        if any(k in blob for k in ("unknownmessage", "unknown message", "notfound",
                                   "not found")):
            return "gone"
        return "failed"

    def _parse_purge(self, text: str):
        """Match a purge command; -> (mode, target_id, rest) or None.

        mode: 'preview' (dry run) or 'confirm' (delete the pending batch).
        Tolerates a leading ping / name mention, e.g. "magbot /del-all @user".
        """
        strip_re = r"^(?:\s*(?:<@[0-9A-Za-z]+>|" + "|".join(
            re.escape(n) for n in sorted(BOT_NAMES)) + r"))+\s*"
        raw = re.sub(strip_re, "", text.strip(), flags=re.IGNORECASE)
        low = raw.lower()
        for alias in sorted(PURGE_ALIASES, key=len, reverse=True):
            if not low.startswith(alias):
                continue
            nxt = raw[len(alias):len(alias) + 1]
            if nxt and (nxt.isalnum() or nxt in "-_"):
                continue
            rest = raw[len(alias):].strip()
            rest_low = rest.lower()
            is_confirm = any(re.search(r"\b" + w + r"\b", rest_low)
                             for w in ("confirm", "yes", "delete", "do it", "go", "y"))
            return (("confirm" if is_confirm else "preview"),
                    self._extract_target_user_id(rest), rest)
        return None

    async def _purge_permission_error(self, channel, server):
        """None if I may delete others' messages here; else a user-facing warning."""
        me_member = server.get_member(self.me.id)
        if me_member is None:
            try:
                me_member = await server.fetch_member(self.me.id)
            except Exception as e:
                return f"⚠️ Couldn't verify my permissions: {str(e)[:160]}"
        try:
            perms = channel.permissions_for(me_member)
        except Exception:
            try:
                perms = channel.permissions_for(me_member, safe=False)
            except Exception as e:
                return f"⚠️ Couldn't verify my permissions: {str(e)[:160]}"
        if not perms.manage_messages:
            return ("⚠️ I need the **Manage Messages** permission in this channel to delete "
                    "other people's messages — grant it to my role and retry.")
        return None

    async def _purge_scan(self, channel, target_id: str):
        """Walk channel history newest-first; collect ids authored by target_id."""
        ids, scanned, before = [], 0, None
        while scanned < PURGE_SCAN_LIMIT:
            page = min(100, PURGE_SCAN_LIMIT - scanned)
            batch = await channel.history(limit=page, before=before,
                                          sort=stoat.MessageSort.latest,
                                          populate_users=False)
            if not batch:
                break
            for m in batch:
                scanned += 1
                if str(getattr(m, "author_id", "") or "") == target_id:
                    ids.append(str(m.id))
            if len(batch) < page:
                break
            before = str(batch[-1].id)
        return ids, scanned

    async def _purge_target_label(self, target_id: str) -> str:
        """Readable target name for replies (never a live mention — no pings)."""
        try:
            u = await self.fetch_user(target_id)
            name = getattr(u, "name", None)
            disc = getattr(u, "discriminator", None)
            if name:
                return f"{name}#{disc}" if disc else str(name)
        except Exception:
            pass
        return f"`{target_id}`"

    async def _purge_delete(self, channel, ids):
        """Delete ids newest-first (bulk for <1-week-old, singles otherwise).

        Returns (deleted, gone, failed, fatal, first_err, left).
        """
        deleted = gone = failed = 0
        fatal = None
        first_err = None
        budget = PURGE_MAX_DELETE
        cutoff = time.time() - PURGE_BULK_MAX_AGE + 120

        async def _one(mid):
            nonlocal deleted, gone, failed, fatal, first_err, budget
            try:
                await self.http.delete_message(channel, mid)
                deleted += 1
                budget -= 1
            except Exception as e:
                kind = self._purge_err_kind(e)
                if kind == "gone":
                    gone += 1
                elif kind == "fatal":
                    fatal = str(e)
                else:
                    failed += 1
                    if first_err is None:
                        first_err = str(e)
            await asyncio.sleep(PURGE_PACE)

        recent = [i for i in ids if self._ulid_time(i) >= cutoff]
        old = [i for i in ids if self._ulid_time(i) < cutoff]

        i = 0
        while i < len(recent) and budget > 0 and not fatal:
            chunk = recent[i:i + min(PURGE_BULK_MAX, budget)]
            i += len(chunk)
            try:
                await self.http.delete_messages(channel, chunk)
                deleted += len(chunk)
                budget -= len(chunk)
            except Exception as e:
                if self._purge_err_kind(e) == "fatal":
                    fatal = str(e)
                    break
                for mid in chunk:  # bulk refused (e.g. age race) — retry one by one
                    if budget <= 0 or fatal:
                        break
                    await _one(mid)
            if not fatal:
                await asyncio.sleep(PURGE_PACE)

        for mid in old:
            if budget <= 0 or fatal:
                break
            await _one(mid)

        left = len(ids) - deleted - gone - failed
        return deleted, gone, failed, fatal, first_err, left

    async def _handle_purge(self, message, text: str) -> bool:
        parsed = self._parse_purge(text)
        if parsed is None:
            return False
        mode, target_id, rest = parsed

        author_id, author_tag = _author_identity(message)
        if author_tag not in ADMIN_ACCOUNTS:
            try:
                await message.reply("🚫 Only admins can use purge commands.")
            except Exception:
                pass
            log.warning(f"Non-admin {author_tag} attempted purge: {text.strip()[:80]}")
            return True

        try:
            channel = message.channel
        except Exception:
            channel = None
        server = None
        if channel is not None:
            try:
                server = message.server
            except Exception:
                server = None
            if server is None:
                try:
                    server = channel.server
                except Exception:
                    server = None
        if channel is None or server is None or not hasattr(channel, "history"):
            try:
                await message.reply("⚠️ Use purge in a server text channel — I need its history to scan.")
            except Exception:
                pass
            return True
        channel_id = str(channel.id)
        now = time.time()

        # Housekeeping: drop expired previews.
        for k in [k for k, v in list(PURGE_PENDING.items()) if v.get("expires", 0) < now]:
            PURGE_PENDING.pop(k, None)

        if mode == "confirm" and not target_id:
            keys = [k for k in PURGE_PENDING if k[0] == channel_id]
            if len(keys) == 1:
                target_id = keys[0][1]
            else:
                hint = (f"{len(keys)} previews pending — mention the user."
                        if len(keys) > 1 else
                        "No pending preview for this channel — run `/del-all @user` first.")
                try:
                    await message.reply(f"⌛ {hint}")
                except Exception:
                    pass
                return True

        if not target_id:
            try:
                await message.reply(
                    "⚠️ Usage: `/del-all @user` (dry run), then `/del-all @user confirm` to delete.\n"
                    "Aliases: `/delall`, `/del_all`, `/purge` — or the word forms `delalldelalldelall` / "
                    "`purgepurgepurge` if Stoat swallows the slash command.")
            except Exception:
                pass
            return True

        err = await self._purge_permission_error(channel, server)
        if err:
            try:
                await message.reply(err)
            except Exception:
                pass
            return True

        key = (channel_id, target_id)

        if mode != "confirm":
            try:
                ids, scanned = await self._purge_scan(channel, target_id)
            except Exception as e:
                log.error(f"Purge scan failed in {channel_id}: {e}")
                try:
                    await message.reply(f"⚠️ Scan failed: {str(e)[:200]}")
                except Exception:
                    pass
                return True
            label = await self._purge_target_label(target_id)
            if not ids:
                try:
                    await message.reply(f"🔎 Scanned the last {scanned} message(s) in "
                                        f"<#{channel_id}> — no messages from {label} found.")
                except Exception:
                    pass
                return True
            cutoff_n = time.time() - PURGE_BULK_MAX_AGE + 120
            recent_n = sum(1 for i in ids if self._ulid_time(i) >= cutoff_n)
            PURGE_PENDING[key] = {"by": author_id, "expires": now + PURGE_CONFIRM_TTL,
                                  "ids": ids}
            extra = (f"\n• One confirm deletes at most {PURGE_MAX_DELETE} — repeat for the rest."
                     if len(ids) > PURGE_MAX_DELETE else "")
            try:
                await message.reply(
                    f"🧹 Dry run in <#{channel_id}> — scanned the latest {scanned} message(s): "
                    f"found **{len(ids)}** from {label}.\n"
                    f"• {recent_n} younger than 1 week → bulk delete\n"
                    f"• {len(ids) - recent_n} older → deleted one by one\n"
                    f"Reply `/del-all @user confirm` within {PURGE_CONFIRM_TTL // 60} min to delete them — "
                    f"nothing has been deleted yet.{extra}")
            except Exception:
                pass
            log.info(f"Purge preview by {author_tag} in {channel_id}: "
                     f"{len(ids)}/{scanned} from {target_id}")
            return True

        entry = PURGE_PENDING.pop(key, None)
        if entry is None or entry.get("expires", 0) < now:
            try:
                await message.reply(f"⌛ No fresh preview for that user in this channel "
                                    f"(previews last {PURGE_CONFIRM_TTL // 60} min). "
                                    f"Run `/del-all @user` first.")
            except Exception:
                pass
            return True

        ids = entry["ids"]
        label = await self._purge_target_label(target_id)
        try:
            await message.react("🧹")
        except Exception:
            pass
        log.info(f"Purge confirmed by {author_tag} in {channel_id}: "
                 f"deleting {len(ids)} message(s) from {target_id}")
        deleted, gone, failed, fatal, first_err, left = await self._purge_delete(channel, ids)
        bits = [f"🧹 Purged **{deleted}** message(s) from {label} in <#{channel_id}>."]
        if gone:
            bits.append(f" {gone} were already gone.")
        if failed:
            bits.append(f" {failed} failed — first error: {str(first_err)[:160]}")
        if fatal:
            bits.append(f" ⚠️ Stopped early: {str(fatal)[:160]} — grant **Manage Messages** and retry.")
        if left:
            bits.append(f" {left} not attempted — one confirm handles {PURGE_MAX_DELETE}; "
                        f"run the preview again to continue.")
        try:
            await message.reply("".join(bits))
        except Exception:
            pass
        log.info(f"Purge result in {channel_id}: deleted={deleted} gone={gone} "
                 f"failed={failed} left={left} fatal={bool(fatal)}")
        return True


    # ── Main message handler ─────────────────────────────────────────────

    async def on_message(self, message, /):
        if message.author_id == self.me.id:
            return
        if hasattr(message.author, 'bot') and message.author.bot:
            return

        # ── Consent filter ─────────────────────────────────────────────
        # If this account opted out of having their data used by the AI,
        # drop the message BEFORE it reaches the bridge. Nothing about it
        # (content or author) is forwarded, logged as context, or acted on.
        _aid, _atag = _author_identity(message)
        _abare = _atag.split("#", 1)[0]  # username without discriminator

        # ── Consent layer (opt-in allowlist) ───────────────────────────
        # Data is collected ONLY from users on the allowlist. Everyone else is
        # blocked here: their message is never relayed to the harness, never
        # stored as context, and never answered. Users who address magbot by
        # name while not opted in get the preset opt-in message (rate-limited).
        _request_raw = (message.content or "") or ""
        # Is this message actually addressed to magbot? Consent commands use the
        # same trigger set as the bot's reply logic (@-ping, name in the text, or
        # a DM/GC), so casual channel chatter can't flip anyone's consent.
        _pinged = self.me.id in (message.mention_ids or [])
        _named = self._is_name_mentioned(_request_raw)
        _addressed = _pinged or _named or self._is_dm_like(getattr(message, "channel", None))
        # Strict command form: only "magbot opt in" / "magbot opt out" (or the
        # same after a ping, or bare in a DM/GC with the bot) is a command.
        _is_dm = self._is_dm_like(getattr(message, "channel", None))

        # Opt-IN requests come first so a brand-new user can join with one
        # command (this also lifts a previous opt-out).
        if consent.optin_requested(_request_raw, pinged=_pinged, is_dm=_is_dm):
            added = consent.allow(_aid, _atag)
            if added:
                log.info("User %s (%s) opted IN — added to allowlist", _atag, _aid)
                _msg = ("yay, thank you! ✨ i've added you to my opt-in list — i'll read "
                        "and reply to your messages from now on. say `magbot opt out` any "
                        "time if you change your mind 💛")
            else:
                log.info("Opt-in requested by already-allowed %s (%s)", _atag, _aid)
                _msg = "you're already opted in — i'm reading & replying to you. ✨ (`magbot opt out` to undo)"
            try:
                await message.reply(_msg, mention=True)
            except Exception:
                pass
            return

        # Opt-out requests are handled here, before the allowlist gate, so
        # anyone (opted in or not) can exclude themselves at any time.
        if consent.optout_requested(_request_raw, pinged=_pinged, is_dm=_is_dm):
            if consent.is_excluded(_aid, _atag):
                log.info("Opt-out requested by already-excluded %s (%s)", _atag, _aid)
                _msg = "You're already opted out of my AI context — your messages aren't used. 👍"
            else:
                added = consent.deny(_aid, _atag)
                if added:
                    log.info("User %s (%s) requested opt-out — hard-blocked, removed from allowlist", _atag, _aid)
                    _msg = ("Done — I've removed you from my opt-in list and won't read or reply to "
                            "your messages. Say `magbot opt in` if you change your mind. 🔒")
                else:
                    _msg = "Hmm, I couldn't update the opt-out list. Please contact the operator. ⚠️"
            try:
                await message.reply(_msg, mention=True)
            except Exception:
                pass
            return

        # Explicit exclusions (optout.list / operator env): always silent.
        if consent.is_excluded(_aid, _atag):
            log.info("Dropped message from consent-excluded account %s (%s) — not relayed to Hermes", _atag, _aid)
            return

        # ── The block layer ───────────────────────────────────────────
        # Not on the allowlist => nothing about this message is collected or
        # answered. If they ping magbot or say its name, they get the preset
        # opt-in message instead of the AI.
        if not consent.is_allowed(_aid, _atag):
            if _pinged or _named:
                if consent.should_prompt(_aid):
                    try:
                        await message.reply(consent.PRESET_OPTIN_MESSAGE, mention=False)
                        consent.mark_prompted(_aid)
                    except Exception as e:
                        log.warning("opt-in prompt failed for %s (%s): %s", _atag, _aid, e)
                    else:
                        log.info("Sent opt-in prompt to %s (%s) (%s)", _atag, _aid,
                                 "ping" if _pinged else "name-mention")
                else:
                    log.info("Opt-in prompt for %s (%s) suppressed (cooldown)", _atag, _aid)
            log.info("Blocked %s (%s): not opted in — message not collected or relayed", _atag, _aid)
            return

        user_text = message.content or ""
        has_attachments = bool(getattr(message, "attachments", []) or [])
        if not user_text.strip() and not has_attachments:
            return

        # Moderation commands run before the Hermes brain.
        moderation_cmd = await self._handle_moderation(message, user_text)
        if moderation_cmd:
            return

        # Purge (bulk-delete one user's messages) — same admin gate as above.
        purge_cmd = await self._handle_purge(message, user_text)
        if purge_cmd:
            return

        is_pinged = self.me.id in (message.mention_ids or [])
        channel_name = self._get_channel_name(message)
        is_name_mentioned = self._is_name_mentioned(user_text)

        if not is_pinged and not is_name_mentioned:
            return

        # Download image attachments so Hermes can see them.
        image_paths = []
        if has_attachments:
            image_paths = await download_attachments(message)

        try:
            await message.react("👀")
        except Exception:
            pass

        # Extract the actual question — strip ping mentions and bot name.
        clean_text = re.sub(r"<@[0-9A-Za-z]+>", "", user_text).strip()
        for name in BOT_NAMES:
            clean_text = re.sub(rf"\b{re.escape(name)}\b", "", clean_text, flags=re.IGNORECASE)
        clean_text = clean_text.strip()
        if not clean_text:
            clean_text = "Hello!"
        if image_paths:
            clean_text += "\n[Attached image(s): " + ", ".join(image_paths) + "]"

        username = getattr(message.author, 'display_name', None) or \
                   getattr(message.author, 'name', None) or \
                   str(message.author_id)
        author_tag = ""
        try:
            author_tag = f"{message.author.name}#{message.author.discriminator}" \
                         if hasattr(message.author, 'discriminator') and message.author.discriminator \
                         else username
        except Exception:
            author_tag = username

        is_admin = author_tag in ADMIN_ACCOUNTS
        trigger = "ping" if is_pinged else "name-mention"
        log.info(f"{trigger} from {author_tag} ({message.author_id}) [admin={is_admin}] in #{channel_name}: {clean_text[:100]}")

        # Safety gate (defense-in-depth; admins bypass).
        if not is_admin and not is_safe(clean_text):
            log.warning(f"Blocked dangerous message from {author_tag}: {clean_text[:100]}")
            try:
                await message.reply(
                    "🚫 I can't help with that — it looks like it could be destructive "
                    "or malicious. I'm happy to help with code, games, questions, "
                    "broadcasting, and other harmless stuff though! >w<"
                )
            except Exception:
                pass
            return

        # Apply any per-user personality override as a note to Hermes.
        # Then prepend recent channel context so the agent sees the thread
        # that led up to this mention (feature restored from the old brain).
        prompt = clean_text
        ov = PERSONALITY_OVERRIDES.get(author_tag)
        if ov:
            prompt = f"[SPECIAL RULE FOR THIS USER]\n{ov}\n\n{clean_text}"

        ctx = await _recent_context(message)
        if ctx:
            prompt = f"{ctx}\n[USER MESSAGE]\n{prompt}"

        response = None
        reasoning = ""
        try:
            async with message.channel.typing():
                response, reasoning = await asyncio.to_thread(
                    bridge.ask_full, prompt, author_tag
                )
        except Exception as e:
            log.error(f"Hermes bridge phase failed: {e}")
        if response is None:
            try:
                response, reasoning = await asyncio.to_thread(
                    bridge.ask_full, prompt, author_tag
                )
            except Exception as e:
                log.error(f"Hermes bridge fallback failed: {e}")
                response = "⚠️ Sorry, I hit an error and couldn't process that. Try again in a moment!"
                reasoning = ""

        # Neutralise tildes so the persona's "~" tics can't render as Stoat
        # strikethrough (single-tilde GFM) and strike out the start of a reply.
        response = sanitize_markdown(response)

        # Thinking traces (Hermes-on-Discord style): the model's reasoning as a
        # small block above the answer. Empty when the model didn't reason or
        # THINKING_MODE=off, so nothing changes for those turns.
        _think = format_thinking(reasoning)
        if _think:
            log.info(
                f"Thinking trace for {author_tag}: {len(reasoning)} chars "
                f"-> {THINKING_MODE} block ({len(_think)} chars rendered)"
            )
            response = f"{_think}\n\n{response}"

        # Split long replies into multiple messages.
        MAX_MSG = 1600
        if len(response) > MAX_MSG:
            chunks = []
            while len(response) > MAX_MSG:
                cut = response.rfind("\n\n", 0, MAX_MSG)
                if cut < MAX_MSG // 2:
                    cut = response.rfind(" ", 0, MAX_MSG)
                if cut < MAX_MSG // 2:
                    cut = MAX_MSG
                chunks.append(response[:cut].strip())
                response = response[cut:].strip()
            if response:
                chunks.append(response)
            response = chunks

        try:
            if isinstance(response, list):
                for i, chunk in enumerate(response):
                    await message.reply(chunk, mention=False)
                    log.info(f"Replied to {author_tag} (part {i+1}/{len(response)}): {chunk[:60]}")
            else:
                await message.reply(response, mention=False)
                log.info(f"Replied to {author_tag}: {response[:80]}")
            try:
                await message.react("✅")
            except Exception:
                pass
        except Exception as e:
            log.error(f"Failed to reply: {e}")
            try:
                _chunks = response if isinstance(response, list) else [response]
                for _chunk in _chunks:
                    try:
                        await message.channel.send(_chunk)
                    except Exception as e2:
                        log.error(f"Fallback send failed for chunk: {e2}")
            except Exception:
                pass


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    log.info("Starting Stoat.chat bot (Hermes ACP backend)...")
    bot = StoatBot()
    try:
        bot.run(STOAT_TOKEN)
    finally:
        bridge.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
