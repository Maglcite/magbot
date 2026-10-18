# magbot

An AI chat bot for [Stoat.chat](https://stoat.chat). It answers when someone pings it or mentions its name in a message. The AI side (memory, web search, browsing, file hosting, image generation, an admin-only shell) runs in a [Hermes Agent](https://github.com/NousResearch/hermes-agent) profile. This repo is the Stoat transport layer around that agent, plus an opt-in consent system and a web UI for driving the bot account.

```
Stoat gateway ──> bot.py ──(JSON lines over stdio)──> fembot_hermes_bridge.py ──ACP──> hermes acp (fembot profile)
                    │                                        │
                    └──────────── consent.py ────────────────┘   (both sides enforce opt-in)
```

## What's in here

| path | what it does |
| --- | --- |
| `bot.py` | Stoat client: ping/name detection, recent-channel context, admin moderation commands (`/del-all`/`/purge`, …), image attachments, safety filter, reply splitting, markdown fixes, bridge watchdog. |
| `fembot_hermes_bridge.py` | Long-lived subprocess that holds one Hermes ACP session and relays requests from `bot.py`. Refuses senders who haven't opted in, even when called directly. |
| `consent.py` | Shared opt-in/opt-out policy: allowlist/blocklist files, strict `magbot opt in` / `magbot opt out` command parsing, preset opt-in prompt with a cooldown. See [docs/CONSENT.md](docs/CONSENT.md). |
| `webui/` | Password-protected browser client (aiohttp + vanilla JS) that logs in as the bot account: servers, channels, DMs, live messages. See [docs/WEBUI.md](docs/WEBUI.md). |
| `hermes-profile/` | Example `SOUL.md` (persona and safety rules) and `config.yaml` for the Hermes profile. |
| `deploy/` | systemd units for the bot, the static file server and the web UI. |
| `tests/` | Consent and command-wording tests (they stub the Stoat SDK, so no network is needed). |

## Requirements

- Python 3.11+
- A Stoat bot account and its token
- Hermes Agent installed, with a profile for the bot (e.g. `~/.hermes/profiles/fembot`)

## Setup

```bash
sudo mkdir -p /opt/stoat-bot && sudo chown "$USER" /opt/stoat-bot
cp -r bot.py consent.py fembot_hermes_bridge.py webui /opt/stoat-bot/
python3 -m venv /opt/stoat-bot/venv
/opt/stoat-bot/venv/bin/pip install -r requirements.txt

cp .env.example /opt/stoat-bot/.env && chmod 600 /opt/stoat-bot/.env
$EDITOR /opt/stoat-bot/.env              # token, self id, admins, public file URL

touch /opt/stoat-bot/optin.list /opt/stoat-bot/optout.list
mkdir -p /opt/stoat-bot/www

# Hermes profile: copy the persona and edit the placeholders (<ADMIN_TAG>, files.example.com)
cp hermes-profile/SOUL.md ~/.hermes/profiles/fembot/SOUL.md
```

Install the units in `deploy/`. `stoat-bot` and `stoat-bot-http` are user units (`~/.config/systemd/user/`). `stoat-webui` is a system unit. Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now stoat-bot stoat-bot-http
sudo systemctl enable --now stoat-webui     # optional web UI
```

Logs: `journalctl --user -u stoat-bot -f`.

## Configuration

Everything comes from environment variables, set in `.env` or in the unit file.

| variable | default | meaning |
| --- | --- | --- |
| `STOAT_BOT_TOKEN` | (required) | Bot token |
| `STOAT_SELF_ID` | (empty) | The bot's own user id, so it never gates itself |
| `STOAT_ADMINS` | (empty) | Comma-separated `name#1234` tags. Admins skip the safety filter and can use moderation commands |
| `STOAT_SHELL_USERS` | (empty) | Non-admins allowed to request non-destructive shell work |
| `BOT_NAMES` | `magbot,mag bot,mag-bot` | Words that count as addressing the bot |
| `FILE_BASE_URL` | `https://example.com` | Public URL that `/opt/stoat-bot/www` is served under |
| `PERSONALITY_OVERRIDES_FILE` | `/opt/stoat-bot/personality_overrides.json` | Optional per-user persona tweaks (see `personality_overrides.example.json`) |
| `RECENT_CONTEXT_LIMIT` | `8` | How many earlier channel messages to pass along as context |
| `THINKING_MODE` | `off` | Show model reasoning: `off`, `blockquote`, `spoiler` or `plain` |
| `HEARTBEAT_INTERVAL` | `300` | Seconds between bridge liveness probes (`0` disables them) |
| `BRIDGE_TIMEOUT` / `PROMPT_TIMEOUT` | `120` / `75` | Bridge and per-prompt timeouts |
| `HERMES_PY`, `BRIDGE_SCRIPT`, `FEMBOT_HERMES_HOME`, `FEMBOT_CWD` | see code | Where Hermes and the bridge live |
| `OPT_IN_FILE`, `OPT_OUT_FILE`, `OPTIN_PROMPT_STATE`, `OPTIN_PROMPT_COOLDOWN` | `/opt/stoat-bot/...`, 12h | Consent state |
| `STOAT_NO_CONTEXT`, `STOAT_NO_CONTEXT_IDS` | (empty) | Silent exclusions that are never relayed to the AI |

## Privacy

The bot only collects messages from users who opted in. Everyone else is ignored, apart from a one-time prompt explaining how to opt in if they address the bot. Opting out takes effect immediately and wins over everything else. Full details are in [docs/CONSENT.md](docs/CONSENT.md).

None of the runtime data is in this repo: the opt-in lists, chat memory, user profiles, logs and the `.env` are excluded and listed in `.gitignore`.

## Tests

```bash
python tests/test_consent.py     # consent module, the bot.on_message gate, the bridge barrier
python tests/test_commands.py    # opt-in/opt-out wording matrix
```

The tests only write to temp files under `/tmp`.
