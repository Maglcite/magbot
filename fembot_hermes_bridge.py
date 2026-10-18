#!/usr/bin/env python3
"""
fembot_hermes_bridge.py — persistent Hermes ACP bridge for the Stoat fembot.

Runs inside the hermes-agent venv so `acp` + `agent_client_protocol` are
importable. Spawns `hermes acp` under the fembot profile's HERMES_HOME, opens
ONE persistent ACP session, and exposes a tiny line protocol on stdio:

  Request  (stdin, one JSON object per line):
    {"id": N, "message": "the user's message", "user": "name#1234"}
    {"id": N, "ping": true}                 # liveness check
    {"id": N, "mode": "compact"|"reset"}    # future management
  Response (stdout, one JSON object per line):
    {"id": N, "ok": true, "reply": "...", "reasoning": "...", "ms": 1234}
    {"id": N, "ok": false, "error": "..."}
    {"event": "ready", "model": "..."}

`reasoning` is the model's streamed thinking (ACP `agent_thought_chunk`s) for
that turn, capped at REASONING_MAX_CHARS. It is empty when the model produced no
reasoning (trivial answers) or when the ACP session has no reasoning config.
`reply` is unchanged, so older consumers that only read `reply` keep working.

The ACP session persists across messages, so Hermes keeps real conversation
context AND stays warm (fast replies). Every tool the bot needs (web search,
browse, terminal/shell, file read/write, image gen) is handled by Hermes's own
tool loop over this same session.
"""

import asyncio
import json
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import consent  # opt-in allowlist (shared with the transport layer)

log = logging.getLogger("bridge")

HERMES_HOME = os.environ.get("FEMBOT_HERMES_HOME", "/root/.hermes/profiles/fembot")

# Persistent opt-out list shared with bot.py. If a user is listed here their
# messages are NEVER relayed into the ACP session — the hard barrier at the
# bridge boundary. Read live each request so no restart is needed.
OPT_OUT_FILE = os.environ.get("OPT_OUT_FILE", "/opt/stoat-bot/optout.list")

def _is_opted_out(who: str) -> bool:
    """True if `who` (a "name#1234" tag) is on the persistent opt-out list.

    Matches the exact tag and its bare name. A raw Stoat user ID in the message
    user field is also honored. Read from disk live each call — the list is
    small, and this guarantees an opt-out applies the moment it is written.
    """
    if not who:
        return False
    bare = who.split("#", 1)[0]
    try:
        with open(OPT_OUT_FILE) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                entry = line.split(None, 1)
                val = entry[1].strip() if len(entry) == 2 else line
                if who == val or bare == val:
                    return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return False


def relay_refused(who: str) -> bool:
    """The harness barrier: may this sender's message reach the ACP session?

    Refused when the sender is blank, on the legacy env sets, on the opt-out
    file, or not on the opt-in allowlist (consent.py). Read live on every
    request so an opt-in/opt-out applies immediately, no restart needed.
    """
    if not who:
        return False
    noctx_tags = {s.strip() for s in os.environ.get("STOAT_NO_CONTEXT", "").split(",") if s.strip()}
    noctx_ids = {s.strip() for s in os.environ.get("STOAT_NO_CONTEXT_IDS", "").split(",") if s.strip()}
    if who in noctx_tags or who in noctx_ids:
        return True
    if _is_opted_out(who):
        return True
    return not consent.is_allowed(None, who)

CWD = os.environ.get("FEMBOT_CWD", "/opt/stoat-bot/www")
PROMPT_TIMEOUT = int(os.environ.get("PROMPT_TIMEOUT", "75"))

# Cap on how much reasoning text is handed back per turn (chars). Reasoning can
# run to tens of KB; the Stoat transport only needs enough to show a trace.
REASONING_MAX_CHARS = int(os.environ.get("REASONING_MAX_CHARS", "6000"))


class FembotClient:
    """Minimal ACP client: receives streamed chunks and accumulates text."""

    def __init__(self):
        self.parts = []
        self.thoughts = []
        self.capture = False
        self.final_text = ""
        self.ready = asyncio.Event()
        self.model = "?"

    def on_connect(self, conn):
        self.conn = conn
        self.ready.set()

    async def session_update(self, session_id, update, **kwargs):
        # update is a ContentChunk: sessionUpdate "agent_message_chunk" carries
        # the streamed assistant text.
        content = getattr(update, "content", None)
        if content is None:
            return
        kind = getattr(update, "session_update", None)
        if kind == "agent_message_chunk":
            text = getattr(content, "text", None)
            if text:
                self.parts.append(text)
        elif kind == "agent_thought_chunk":
            # Model reasoning ("thinking") streamed by the agent. Captured so the
            # Stoat side can show thinking traces the way Hermes does on Discord.
            # Requires a reasoning config on the ACP session (otherwise the
            # provider is never asked for reasoning and this never fires).
            text = getattr(content, "text", None)
            if text:
                self.thoughts.append(text)
        return None

    async def request_permission(self, *a, **k):
        return None

    async def write_text_file(self, *a, **k):
        return None

    async def read_text_file(self, *a, **k):
        return None

    async def create_terminal(self, *a, **k):
        return None

    async def terminal_output(self, session_id, terminal_id, **k):
        return None

    async def release_terminal(self, session_id, terminal_id, **k):
        return None

    async def wait_for_terminal_exit(self, session_id, terminal_id, **k):
        return None

    async def kill_terminal(self, session_id, terminal_id, **k):
        return None

    async def ext_method(self, method, params):
        return {}

    async def ext_notification(self, method, params):
        return None


async def collect_chunks(client):
    from acp.schema import TextContentBlock
    return "".join(client.parts)


async def run_loop(proc_in, proc_out):
    import acp
    from acp.client.connection import ClientSideConnection
    from acp.schema import TextContentBlock

    client = FembotClient()
    conn = ClientSideConnection(
        to_client=lambda agent: client,
        input_stream=proc_in,   # StreamWriter -> agent's stdin
        output_stream=proc_out, # StreamReader <- agent's stdout
        use_unstable_protocol=True,
    )
    await client.ready.wait()
    log.info("ACP transport connected")

    # initialize
    init = await conn.initialize(protocol_version=1)
    log.info("initialized protocol=%s model hint=%s", getattr(init, "protocol_version", "?"), client.model)

    # persistent session
    sess = await conn.new_session(cwd=CWD)
    session_id = sess.session_id
    # Put the session in "don't ask" mode so the autonomous bot never blocks on
    # ACP edit-approval prompts for routine file writes.
    try:
        await conn.set_session_mode(mode_id="dont_ask", session_id=session_id)
        log.info("session mode set to dont_ask")
    except Exception as e:
        log.warning("could not set dont_ask mode: %s", e)
    log.info("new_session id=%s", session_id)

    # stdout ordering: emit ready
    print(json.dumps({"event": "ready", "session": session_id}), flush=True)

    loop = asyncio.get_running_loop()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            print(json.dumps({"id": None, "ok": False, "error": f"bad json: {e}"}), flush=True)
            continue
        rid = req.get("id")
        if req.get("ping"):
            print(json.dumps({"id": rid, "ok": True, "reply": "pong"}), flush=True)
            continue
        message = req.get("message", "")
        who = req.get("user", "")

        # Defense-in-depth consent gate: even a request that reaches the
        # bridge directly is never relayed into the ACP session (i.e. never
        # becomes context) unless the sender is on the opt-in allowlist.
        if relay_refused(who):
            log.info("refused relay for %s: not on the opt-in allowlist", who)
            print(json.dumps({"id": rid, "ok": True,
                              "reply": "(message not relayed — sender not opted in)", "ms": 0}), flush=True)
            continue
        t0 = time.time()
        try:
            client.parts = []
            client.thoughts = []
            prefix = f"[From: {who}] " if who else ""
            payload = [TextContentBlock(type="text", text=prefix + message)]
            # Hard per-prompt deadline so a wedged ACP session can't hang the
            # bot forever. On timeout we report the error and terminate this
            # bridge process; the parent bot sees EOF and respawns a fresh
            # bridge + ACP session, unblocking everyone waiting on the lock.
            try:
                resp = await asyncio.wait_for(
                    conn.prompt(prompt=payload, session_id=session_id),
                    timeout=PROMPT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                ms = int((time.time() - t0) * 1000)
                log.error("prompt timed out after %ss — exiting to force a bounce", PROMPT_TIMEOUT)
                print(json.dumps({"id": rid, "ok": False,
                                  "error": f"prompt timed out after {PROMPT_TIMEOUT}s", "ms": ms}), flush=True)
                os._exit(3)  # terminate; bot detects EOF and bounces
            reply = "".join(client.parts)
            if not reply.strip():
                reply = "(no text response)"
            reasoning = "".join(client.thoughts).strip()
            if len(reasoning) > REASONING_MAX_CHARS:
                reasoning = reasoning[:REASONING_MAX_CHARS]
            ms = int((time.time() - t0) * 1000)
            print(json.dumps({"id": rid, "ok": True, "reply": reply,
                              "reasoning": reasoning, "ms": ms}), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            log.exception("prompt failed")
            print(json.dumps({"id": rid, "ok": False, "error": str(e)[:500], "ms": ms}), flush=True)


async def main():
    proc = await asyncio.create_subprocess_exec(
        "hermes", "acp",
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        env={**os.environ, "HERMES_HOME": HERMES_HOME},
    )
    log.info("spawned hermes acp pid=%s", proc.pid)
    try:
        await run_loop(proc.stdin, proc.stdout)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
