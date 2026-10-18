#!/usr/bin/env python3
"""Verify the opt-in consent layer (unit + transport-gate level).

Run with temp files so nothing real is touched:
  OPT_IN_FILE=/tmp/t-optin.list OPT_OUT_FILE=/tmp/t-optout.list \
  OPTIN_PROMPT_STATE=/tmp/t-prompt.json python test_consent.py
"""
import asyncio, os, sys, types, shutil

# Fresh temp consent state on every run (never touch the real files).
_here = os.path.dirname(os.path.abspath(__file__))
_src = os.environ.get("CONSENT_SRC", os.path.join(_here, "fixtures", "optin.list"))
os.environ.setdefault("OPT_IN_FILE", "/tmp/t-optin.list")
os.environ.setdefault("OPT_OUT_FILE", "/tmp/t-optout.list")
os.environ.setdefault("OPTIN_PROMPT_STATE", "/tmp/t-prompt.json")
os.environ.setdefault("STOAT_SELF_ID", "01DDDDDDDDDDDDDDDDDDDDDDD4")
shutil.copyfile(_src, os.environ.get("OPT_IN_FILE", "/tmp/t-optin.list"))
for _p in (os.environ.get("OPT_OUT_FILE", "/tmp/t-optout.list"),
           os.environ.get("OPTIN_PROMPT_STATE", "/tmp/t-prompt.json")):
    try:
        os.remove(_p)
    except FileNotFoundError:
        pass

# --- stub the stoat SDK so bot.py imports without connecting -----------------
stub = types.ModuleType("stoat")
class _Client:
    def __init__(self, *a, **k): pass
stub.Client = _Client
stub.Message = object
stub.User = object
stub.utils = types.SimpleNamespace()
sys.modules["stoat"] = stub

sys.path.insert(0, os.path.dirname(_here))
import consent  # noqa: E402

FAILS = []
def check(label, cond):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)

ALICE = "01AAAAAAAAAAAAAAAAAAAAAAA1"
BOB = "01BBBBBBBBBBBBBBBBBBBBBBB2"
CAROL = "01CCCCCCCCCCCCCCCCCCCCCCC3"
BOT = "01DDDDDDDDDDDDDDDDDDDDDDD4"
STR = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"        # used for the toggle section
STR2 = "01YYYYYYYYYYYYYYYYYYYYYYYYYY"       # fresh, never toggled
STR3 = "01XXXXXXXXXXXXXXXXXXXXXXXXXX"       # only for the cooldown unit check

print("== consent module ==")
check("grandfathered alice (id+tag) allowed", consent.is_allowed(ALICE, "alice#1001"))
check("grandfathered bob (bare id) allowed", consent.is_allowed(BOB, None))
check("grandfathered carol allowed", consent.is_allowed(CAROL, "carol#1003"))
check("grandfathered user matches by OLD tag (rename-proof)",
      consent.is_allowed(None, "dana_old#1004"))
check("magbot itself allowed", consent.is_allowed(BOT, "magbot"))
check("unknown new user blocked", not consent.is_allowed(STR2, "stranger2#0001"))
check("unknown user blocked by tag only", not consent.is_allowed(None, "stranger2#0001"))
check("strict form detected ('magbot opt in')", consent.optin_requested("magbot opt in"))
check("unaddressed phrase ignored ('ok, opt in')", not consent.optin_requested("ok, opt in"))
check("bang command no longer accepted", not consent.optin_requested("!optin"))
check("opt-in not falsely detected on chit-chat",
      not consent.optin_requested("i ate an option once"))
check("strict form detected ('magbot opt out')", consent.optout_requested("magbot opt out"))
check("unaddressed phrase ignored ('yes please opt me out')", not consent.optout_requested("yes please opt me out"))
check("DM + bare phrase honoured", consent.optout_requested("opt out", is_dm=True))
check("opt-out not falsely detected", not consent.optout_requested("optional"))
ok = consent.should_prompt(STR3)
consent.mark_prompted(STR3)
check("prompt allowed once, then cooldown", ok and not consent.should_prompt(STR3))

print("== consent toggles (temp files) ==")
check("new user opt-in adds to allowlist", consent.allow(STR, "stranger#0001"))
check("...and is allowed afterwards", consent.is_allowed(STR, "stranger#0001"))
check("second opt-in is a no-op", not consent.allow(STR, "stranger#0001"))
check("opt-out removes from allowlist", consent.deny(STR, "stranger#0001"))
check("...and hard-blocks",
      not consent.is_allowed(STR, "stranger#0001") and consent.is_excluded(STR, "stranger#0001"))
check("re-opting-in lifts the block",
      consent.allow(STR, "stranger#0001") and consent.is_allowed(STR, "stranger#0001"))
check("opted-in user no longer excluded", not consent.is_excluded(STR, "stranger#0001"))

print("== transport gate (real bot.on_message) ==")
import bot  # noqa: E402

class FakeAuthor:
    def __init__(self, name, disc, is_bot=False):
        self.name, self.discriminator, self.bot = name, disc, is_bot

class FakeMsg:
    def __init__(self, aid, name, disc, content, mentions=None, is_bot=False):
        self.author_id, self.author = aid, FakeAuthor(name, disc, is_bot)
        self.content, self.mention_ids = content, (mentions or [])
        self.id = "01M" + "0" * 23
        self.attachments = []
        self.channel = types.SimpleNamespace(
            history=self.hist, typing=self.typing, send=self.send, name="testing")
        self.replies, self.reacts, self.sent = [], [], []
    async def hist(self, limit=8, before=None): return []
    def typing(self):
        class C:
            async def __aenter__(s): return None
            async def __aexit__(s, *a): return False
        return C()
    async def reply(self, text, mention=False): self.replies.append(text); return self
    async def react(self, emoji): self.reacts.append(emoji)
    async def send(self, text): self.sent.append(text)

class FakeBridge:
    def __init__(self): self.calls = []
    def ask_full(self, prompt, user):
        self.calls.append((prompt, user))
        return ("(stub reply from the harness)", "")
    def close(self): pass

async def main():
    b = bot.StoatBot()
    b.me = types.SimpleNamespace(id=BOT)
    fb = FakeBridge()
    bot.bridge = fb

    def calls(): return len(fb.calls)

    # 1) unknown user, plain chatter -> fully silent, nothing collected
    n = calls(); m = FakeMsg(STR2, "stranger2", "0001", "hello everyone")
    await b.on_message(m)
    check("blocked: no reply", m.replies == [])
    check("blocked: no reaction", m.reacts == [])
    check("blocked: bridge never called", calls() == n)
    check("blocked: not added to the allowlist", not consent.is_allowed(STR2, "stranger2#0001"))

    # 2) unknown user says the bot's name -> preset opt-in message, no AI
    n = calls(); m2 = FakeMsg(STR2, "stranger2", "0001", "hey magbot can you help me?")
    await b.on_message(m2)
    check("name-mention: preset opt-in message sent", m2.replies == [consent.PRESET_OPTIN_MESSAGE])
    check("name-mention: bridge never called", calls() == n)
    check("name-mention: no reaction either", m2.reacts == [])

    # 3) same user again inside the cooldown -> no second prompt
    n = calls(); m3 = FakeMsg(STR2, "stranger2", "0001", "magbot?")
    await b.on_message(m3)
    check("cooldown: no repeat prompt", m3.replies == [])
    check("cooldown: still nothing relayed", calls() == n)

    # 4) unknown user pings the bot -> preset opt-in message
    os.remove(os.environ.get("OPTIN_PROMPT_STATE", "/tmp/t-prompt.json"))
    n = calls(); m4 = FakeMsg(STR2, "stranger2", "0001", "<@%s> hello!" % BOT, mentions=[BOT])
    await b.on_message(m4)
    check("ping: preset opt-in message sent", m4.replies == [consent.PRESET_OPTIN_MESSAGE])
    check("ping: bridge never called", calls() == n)

    # 5) the stranger opts in -> confirmation, still no AI turn
    n = 0; m5 = FakeMsg(STR2, "stranger2", "0001", "magbot opt in")
    await b.on_message(m5)
    check("opt-in command: confirmed to user", len(m5.replies) == 1 and "opt" in m5.replies[0].lower())
    check("opt-in command: no AI turn", calls() == n)
    check("opt-in command: now allowed", consent.is_allowed(STR2, "stranger2#0001"))

    # 6) same user now pings -> real AI turn through the bridge
    n = calls(); m6 = FakeMsg(STR2, "stranger2", "0001", "<@%s> are you there?" % BOT, mentions=[BOT])
    await b.on_message(m6)
    check("opted-in: bridge called once", calls() == n + 1)
    check("opted-in: reply delivered", any("stub reply" in r for r in m6.replies))

    # 7) grandfathered user still works untouched
    n = calls(); m7 = FakeMsg(ALICE, "alice", "1001", "magbot ping")
    await b.on_message(m7)
    check("grandfathered user: bridge called", calls() == n + 1)

    # 8) opt-out from an allowed user -> hard block + confirmation
    m8 = FakeMsg(ALICE, "alice", "1001", "magbot please opt me out")
    await b.on_message(m8)
    check("opt-out: confirmation sent", len(m8.replies) == 1)
    check("opt-out: now excluded", consent.is_excluded(ALICE, "alice#1001"))
    n = calls(); m9 = FakeMsg(ALICE, "alice", "1001", "magbot hello?")
    await b.on_message(m9)
    check("excluded user: silent, no AI, no prompt", calls() == n and m9.replies == [])

    # 7) opted-in user says "magbot opt me out" -> opted out, no AI turn
    n = calls(); m7 = FakeMsg(STR2, "stranger2", "0001", "magbot opt me out")
    await b.on_message(m7)
    check("addressed opt-out: confirmation sent", len(m7.replies) == 1)
    check("addressed opt-out: no AI turn", calls() == n)
    check("addressed opt-out: user now blocked", not consent.is_allowed(STR2, "stranger2#0001"))

    # restore mag in the temp allowlist for reruns
    consent.allow(ALICE, "alice#1001")

asyncio.run(main())

print("== harness barrier (fembot_hermes_bridge.relay_refused) ==")
import fembot_hermes_bridge as brg  # noqa: E402
check("allowed user passes the barrier", not brg.relay_refused("alice#1001"))
check("rename-proof old tag passes", not brg.relay_refused("dana_old#1004"))
check("unknown user refused", brg.relay_refused("nobody-here#0002"))
check("raw user id of an unknown user refused", brg.relay_refused(STR2))
check("blank sender not refused (bridge ping/keepalive)", not brg.relay_refused(""))
os.environ["STOAT_NO_CONTEXT"] = "EnvBlocked#0001"
check("legacy env ignore-list still enforced", brg.relay_refused("EnvBlocked#0001"))
del os.environ["STOAT_NO_CONTEXT"]
consent.deny(ALICE, "alice#1001")
check("opted-out user refused at the barrier", brg.relay_refused("alice#1001"))
consent.allow(ALICE, "alice#1001")
check("re-opted-in user passes again", not brg.relay_refused("alice#1001"))
print()
print("FAILURES:", FAILS if FAILS else "none — all checks passed")
sys.exit(1 if FAILS else 0)
