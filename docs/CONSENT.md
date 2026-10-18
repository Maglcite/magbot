# magbot consent layer (opt-in)

**Policy (operator directive, 2026-09-24):** magbot collects data **only** from
users on the opt-in allowlist. For everyone else its messages are never
relayed to the AI, never stored as context, and never answered. A user who has
not opted in but addresses magbot (ping or name mention) gets a preset
**opt-in message** telling them how to consent; nothing else happens.

Grandfathering: everyone who had already used magbot counts as opted in.

## Where the barrier lives
1. **Transport gate — `bot.py`, `on_message()`** (before context/memory/bridge):
   - opt-in / opt-out commands are handled *first*, so anyone can consent or
     withdraw with one message (opt-out also lifts a previous opt-in and vice
     versa);
   - `consent.is_excluded()` → silent drop (hard blocklist: `optout.list` + operator env);
   - `not consent.is_allowed()` → drop. If the message pings magbot or says its
     name → send `consent.PRESET_OPTIN_MESSAGE` (once per user per 12 h, state in
     `optin-prompted.json`). No reaction, no context fetch, no bridge call;
   - `_recent_context()` only includes authors who are allowed (plus magbot itself),
     so a non-consenting user's older messages can never ride along as context.
2. **Harness barrier — `fembot_hermes_bridge.py`, `relay_refused(who)`**: even a
   request that reaches the bridge directly (bypassing the transport, e.g. a bug
   or a stray writer) is refused with `"(message not relayed — sender not opted in)"`
   *before* anything touches the ACP session.

Both layers read the lists live on every message → edits apply instantly, no restart.

## Files
| file | role |
| --- | --- |
| `optin.list` | allowlist. One entry per line: `ID <ulid>` and/or `TAG name#1234`; `#` starts a comment line. |
| `optout.list` | hard blocklist (people who asked to be excluded). Same format. |
| `optin-prompted.json` | last time each user got the opt-in prompt (12 h cooldown). |
| `consent.py` | the shared layer (policy, matching, toggles, preset message). |

Matching is by account **id** first, then by tag (exact, then bare name), so a
renamed user keeps their consent state. A tag given without a discriminator
(`name`) also clears stale `name#old` variants when toggling.

## What counts as a command (wording rules, STRICT)

**The only accepted form is naming magbot and asking: `magbot opt in` / `magbot opt out`.**
(`mag bot`, `mag-bot`, `fembot` and an @-ping count as naming it; inside a DM/GC
with magbot the phrase alone is enough, since there the message can only be
aimed at it.)

* The phrase must sit within 40 chars **after** the name — `magbot is great,
  anyway i opt out of that newsletter` is *not* a command.
* Guards still apply: negations (`magbot i don't want to opt out`) and advice
  questions (`magbot, should we opt out of that?`) are not commands.
* **Not accepted any more** (operator directive 2026-09-24): bare `opt out` /
  `opt in` (even as a whole message), `!optin` / `!optout`, inferred privacy
  statements like "stop reading my messages", and unwrapped loose phrasing.
  No command, no state change — tradeoff: a user who says "stop reading my
  messages" without naming magbot is now ignored, so the preset message and
  both confirmations always spell out the exact command.
* Confirmation replies teach it: opt-in → "…say `magbot opt out` to undo",
  opt-out → "…say `magbot opt in` if you change your mind".

Both directions always reply with a confirmation. Opt-out also removes them from
the allowlist and stays a hard block until they opt back in.
→ reply: "yay, thank you! ✨ …" on opt-in; a confirmation on opt-out.

## Grandfathering
Sources merged into the allowlist:
1. `journalctl --user -u stoat-bot` lines `ping|name-mention from <tag> (<id>)` (authoritative id↔tag),
2. the fembot Hermes profile transcripts (`$FEMBOT_HERMES_HOME/state.db`, `[From: …]`),
3. legacy `chat_memory.json` contacts + `channel_memory.json` authors (old brain),
   with names resolved to ids via the servers'/DM member lists.

 Junk/placeholder tags (heartbeat,
test identities) were filtered out.

## Operator tasks
* **Add someone:** append `ID <ulid>` (best) and/or `TAG name#1234` to `optin.list`.
* **Remove someone:** delete their lines, or let them say “opt out”.
* **Silently exclude without telling them:** add to `optout.list`.
* **Watch it work:**
  `journalctl --user -u stoat-bot -f | grep -E "Consent|opt-in prompt|Blocked|opted"`.
* **Test suites (temp files only, never touch the real lists):**
  `python tests/test_consent.py` and `python tests/test_commands.py`.
* *Test-script hygiene:* if you smoke-test live, only delete messages authored by
  your probe webhook or magbot — never blanket-delete "everything new" (a bystander chatting during a
  run would lose their messages).
