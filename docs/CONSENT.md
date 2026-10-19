# magbot consent (opt-in)

**Policy (operator directive, 2026-09-24):** magbot collects data only from users who have opted in.
For everyone else, its messages are never sent to the AI model, never stored as context, and never
answered. A user who has not opted in but addresses magbot (ping or name) gets a preset opt-in message
that explains how to consent. Nothing else happens.

Grandfathering: everyone who had used magbot before the opt-in system counts as opted in. Their
entries are carried over in `optin.list`.

Code: `magbot/consent.py`. It holds the `Consent` class (lists, toggles, prompt cooldown),
`parse_command()` (the consent command wording), and `PRESET_OPTIN_MESSAGE` / `optin_message(config)`.

## Where the barrier is

The gate is `on_message` in `magbot/bot.py`. It runs in this order, and the order is part of the policy:

1. Ignore the bot's own messages and other bots.
2. Build the author (`identity.from_message`) and work out whether the message pinged the bot, named
   it, or came in a DM.
3. Consent command (`consent.parse_command`). `in` calls `allow()`. `out` calls `deny()` and then
   `memory.forget_user()`. Each sends a confirmation and stops. This runs before the exclusion check,
   so an opted-out user can still opt back in.
4. `is_excluded()`: drop silently.
5. `not is_allowed()`: if the message pinged or named the bot and `should_prompt()` is true, send the
   preset message and call `mark_prompted()`. Then stop. No reaction, no context, no model call.
6. Empty message with no attachments: stop.
7. Commands (`/ban`, `/purge`, ...): handled, then stop.
8. Not addressed: stop.
9. Non-admin with text that fails the safety filter: refuse, then stop.
10. Context gathering. Recent channel messages are included only from authors for whom
    `is_allowed()` is true (and from the bot itself). A non-consenting user's older messages can't
    reach the model as context.

Every check reads the lists again, so a change takes effect at once, without a restart.

`is_allowed()` answers in this order: the bot's own id is allowed; an excluded user is not; then
`consent.always_allow`; then `optin.list`. Matching is by account id first, then by tag (exact, then
bare name, case-insensitive for tags), so a renamed user keeps consent.

## Files

All files live in `bot.data_dir` (default `/opt/magbot/data`). The names are fixed.

| file | role |
| --- | --- |
| `optin.list` | allowlist. One entry per line: `ID <user id>` and/or `TAG name#1234`. |
| `optout.list` | blocklist of users who opted out. Same format. |
| `optin-prompted.json` | `{user_id: unix time}` of the last opt-in prompt, for the cooldown. |

List format rules:

- `#` starts a comment line. Blank lines are ignored.
- `ID <value>` and `TAG <value>` are explicit. A bare entry is an id if it is longer than 12
  characters, otherwise a tag.
- A tag given without a discriminator (`name`) matches any `name#NNNN`. When a user toggles consent,
  the bare form also drops stale `name#old` lines.
- A long id written as `TAG <id>` also counts as an id. This is a quirk carried over from the legacy
  bot, kept so old files keep working.

Writes are atomic: the new contents go to a temporary file, which then replaces the list with
`os.replace`. Missing files count as empty. A file that can't be read is logged and read as empty for
checks. It is never rewritten in that state, so a read error can't wipe a list.

## Configuration

The `[consent]` table in `config.toml` replaces the old environment variables:

```toml
[consent]
prompt_cooldown = 43200          # seconds between opt-in prompts to one user (12 hours)
always_allow = ["alice#1001", "01J5ZQ7K8M9N0P1Q2R3S4T5V6W"]   # counts as opted in, no list line needed
always_exclude = ["bob#2002"]    # never collected, never answered, never prompted
```

- `always_allow` and `always_exclude` take tags or user ids, using the same rules as the list files.
- `always_exclude` can't be lifted by the user's own opt-in. Only removing it from `config.toml`
  does that. An opt-in is still written to `optin.list`, but the exclusion wins.
- `always_allow` is checked after exclusions, so an excluded user is still excluded.

## What counts as a consent command

The only accepted form names magbot and asks directly: `magbot opt in` or `magbot opt out`.

- The name can be any entry in `bot.names`. Spaces inside a name are flexible, so `mag bot` also
  matches `magbot`. Names match as whole words, in any case.
- The phrase must come within 40 characters after the name. `magbot is great, anyway I opt out of that
  newsletter` is not a command.
- A ping on the bot, or a DM with it, also works: the phrase counts without a name, since the message
  can only be aimed at the bot. The phrase is checked wherever it appears in that message.
- The opt-in phrase is checked before the opt-out phrase.
- Negations are not commands: "I don't want to opt out", "no need to opt me out", "won't opt out".
- Advice questions are not commands: "magbot, should we opt out of that?"

Not accepted:

- Bare `opt out` or `opt in`, even as a whole message.
- Prefix commands such as `!optin` and `!optout`.
- Privacy statements that don't name the bot, such as "stop reading my messages".

Being strict costs something: a user who writes "stop reading my messages" without naming the bot is
not treated as opting out. The preset message therefore spells out the exact command, and the
confirmations name the opposite command (`magbot opt out` after opting in, `magbot opt in` after opting
out), as the legacy bot did.

## Opt-in prompt

A user who addresses magbot without being opted in gets the preset message, once per cooldown
(`consent.prompt_cooldown`, 12 hours by default). The state is kept in `optin-prompted.json`. The text
uses the first entry of `bot.names`, via `optin_message(config)`. It is not the raw constant, which
still contains a `{name}` placeholder.

## Opt-out

Opting out does three things, in this order:

1. The user is added to `optout.list` and removed from `optin.list`.
2. Their saved memories are deleted from the memory database (`memory.forget_user`). That covers
   every memory about them. Global notes are not theirs, so they stay.
3. The confirmation is sent.

Opting out is a hard block until the user opts in again. There is no partial opt-out.

## Operator tasks

- **Add someone:** append `ID <user id>` (preferred) and/or `TAG name#1234` to `optin.list`. The change
  applies to the next message.
- **Remove someone:** delete their lines from `optin.list`, or let them say `magbot opt out`.
- **Exclude someone silently:** add them to `optout.list`, or to `consent.always_exclude` in
  `config.toml` (which needs a restart).
- **Check what is loaded:** on ready, the bot logs the number of allowed ids and tags and of excluded
  entries (`Consent.counts()`).
- **Watch consent activity:** `journalctl --user -u magbot -f | grep -i consent`. The log lines are
  `consent: opted in ...`, `consent: opted out ...` and `consent: could not read ...`.
- **Back up the lists** with the rest of `data_dir`. They are the only record of who opted in.

Tests use temporary files only and never touch the real lists:

```bash
.venv/bin/python -m pytest -q tests/test_consent.py tests/test_consent_commands.py
```
