# Migrating from the Hermes-based bot

This moves a running deployment of the old bot to the rewrite. The old setup is:

- code and data in `/opt/stoat-bot`
- two user units, `stoat-bot.service` (the bot) and `stoat-bot-http.service` (the file server)
- a Hermes Agent profile, usually `~/.hermes/profiles/fembot`, holding the model settings and persona
- the web UI as the system unit `stoat-webui.service`

The new setup is one process, `python -m magbot`, in `/opt/magbot`, with `config.toml` for settings
and `.env` for secrets.

Old file paths below are written as `/opt/stoat-bot`. Use your own if they differ. Do not delete
anything from the old setup until the new one has run cleanly for a while.

## What is gone

| old component | status |
| --- | --- |
| Hermes Agent and its profile | Gone. magbot calls the OpenAI-compatible API itself. The profile is no longer read. |
| `fembot_hermes_bridge.py` (ACP bridge) | Gone. There is no subprocess. |
| The bridge watchdog and heartbeat (`HEARTBEAT_*`) | Gone. No subprocess can die separately, so nothing needs watching. |
| Hermes memories and sessions | Not migrated. They live in the Hermes profile. magbot's memory starts empty. |
| `chat_memory.json`, `channel_memory.json` (old brain) | Not migrated. |
| `hermes-profile/` in the repo | Replaced by `config.toml` (model) and `persona.md` (persona). |

The consent lists are migrated, see below. The grandfathered users are already in `optin.list`.

## Settings: environment variables to config.toml

The old settings came from environment variables in `stoat-bot.service` and `.env`. In the new bot,
they are in `config.toml` (non-secret) or `.env` (secret). Use `config.example.toml` as the reference.

| old variable | new setting | notes |
| --- | --- | --- |
| `STOAT_BOT_TOKEN` | `.env`: `STOAT_BOT_TOKEN` | Unchanged. Secret, so it stays in the environment. |
| `STOAT_SELF_ID` | automatic | The bot reads its own id on ready. Delete this line. |
| `STOAT_ADMINS` | `bot.admins` | Now a TOML list of `name#1234` tags or user ids. Ids also work. |
| `STOAT_SHELL_USERS` | `bot.shell_users` | TOML list, same format. |
| `BOT_NAMES` | `bot.names` | TOML list. Default: `["magbot", "mag bot", "mag-bot", "fembot"]`. |
| `FILE_BASE_URL` | `bot.file_base_url` | Public URL of `www_dir`. |
| `PERSONALITY_OVERRIDES_FILE` | `bot.overrides_file` | Same JSON format. Relative paths are relative to `config.toml`. |
| `RECENT_CONTEXT_LIMIT` | `bot.recent_context` | Default 8, unchanged. |
| `THINKING_MODE` | `bot.thinking_mode` | `off`, `blockquote`, `spoiler` or `plain`. |
| `THINKING_MAX_LINES` | `bot.thinking_max_lines` | Default 15. |
| `THINKING_MAX_CHARS` | `bot.thinking_max_chars` | Default 1200. |
| `OPT_IN_FILE` | fixed name: `bot.data_dir/optin.list` | No setting. Copy the file to `data_dir`. |
| `OPT_OUT_FILE` | fixed name: `bot.data_dir/optout.list` | No setting. Copy the file to `data_dir`. |
| `OPTIN_PROMPT_STATE` | fixed name: `bot.data_dir/optin-prompted.json` | No setting. Copy the file to `data_dir`. |
| `OPTIN_PROMPT_COOLDOWN` | `consent.prompt_cooldown` | Seconds. Default 43200 (12 hours). |
| `STOAT_OPTIN`, `STOAT_OPTIN_IDS` | `consent.always_allow` | One list for tags and ids. |
| `STOAT_NO_CONTEXT`, `STOAT_NO_CONTEXT_IDS` | `consent.always_exclude` | One list for tags and ids. |
| `FEMBOT_CWD` | `bot.www_dir` | The shell tool's working directory is now `www_dir`. |
| `PROMPT_TIMEOUT`, `BRIDGE_TIMEOUT` | `llm.timeout` | One limit for a whole reply. See the note below. |
| `REASONING_MAX_CHARS` (bridge) | `bot.thinking_max_chars` | The old cap was 6000. The new default is 1200. |
| `HERMES_PY`, `BRIDGE_SCRIPT`, `FEMBOT_HERMES_HOME` | gone | No Hermes, no bridge. |
| `HEARTBEAT_INTERVAL`, `HEARTBEAT_PROMPT` | gone | No heartbeat. |
| `PURGE_SCAN_LIMIT`, `PURGE_CONFIRM_TTL`, `PURGE_PACE`, `PURGE_MAX_DELETE` | not settings | Now constants at the top of `magbot/commands/purge.py`. Change them there. |

Two values to check:

- **The timeout.** The old unit set `PROMPT_TIMEOUT=300` and `BRIDGE_TIMEOUT=330`. The new default
  `llm.timeout` is 120 seconds. To keep the old budget, set `timeout = 300.0` in the `[llm]` table.
- **Consent after the copy.** Opt-in changes made after you copy the lists (see step 3 below) will be
  missing. Copy them again at cutover, as in step 6.

## Model: Hermes profile to `[[llm.providers]]`

The Hermes profile's `config.yaml` (usually `~/.hermes/profiles/fembot/config.yaml`) held the model
settings. Map them like this:

| Hermes setting | new setting |
| --- | --- |
| `model.default` | `[[llm.providers]]` `model` |
| `model.base_url` | `base_url` (the endpoint must be OpenAI-compatible) |
| `model.key_env` | `api_key_env`. Keep the same variable name and put the key in `.env`. |
| `model.reasoning_effort`, `agent.reasoning_effort` | `reasoning_effort`, for example `"low"` |
| `model.provider: custom` | no equivalent needed. Any endpoint with `base_url` works. |
| `fallback_providers` (each entry) | a second and later `[[llm.providers]]` block, in the same order |
| `platform_toolsets` | the tools registry. Turn unwanted tools off with `tools.disabled`. |
| the `vision` toolset | `vision = true` on the provider, if the model accepts images |
| the `image_gen` toolset | `tools.image_base_url`, `tools.image_model`, `tools.image_api_key_env` |
| `approvals.mode: off` | no equivalent. Shell access is limited to `bot.shell_users` (admins don't get it automatically), plus the safety filter for non-admins. |

Example, for a provider that was `custom` with a key in `CLINE_API_KEY` and reasoning effort `low`:

```toml
[[llm.providers]]
name = "primary"
base_url = "https://api.example.com/v1"     # the Hermes model.base_url
model = "your-model-name"                   # the Hermes model.default
api_key_env = "CLINE_API_KEY"               # the Hermes model.key_env; the key goes in .env
reasoning_effort = "low"
vision = false
```

The Hermes profile's toolsets have no direct equivalents for browser, computer use, cron, delegation,
kanban, skills, TTS or Spotify. They are not carried over. The tools that replace the useful ones are
`web_search`, `fetch_url`, `write_file`, `read_file`, `list_files`, `run_shell`, `remember` and `forget`.

## Persona: SOUL.md to persona.md

The new persona is `persona.md` in the repository, a complete default. Start from it, and move your
own personality text in from `SOUL.md`. Then fix these parts of the copied text:

- Use the placeholders. Replace the bot's name with `{bot_name}`, and replace the example files URL
  (`https://files.example.com/...`) with `{file_base_url}`. Both are filled in at startup.
- Remove the admin tag. `<ADMIN_TAG>` and similar lines are not needed: magbot adds a "Current
  speaker" section to every request, which says whether the speaker is an admin or may use the shell.
- Remove the host details: `/opt/stoat-bot`, the service names, and anything about Hermes. The persona
  should not describe the machine.
- Keep the rules about not disclosing the model, and the tone.
- Remove rules that the tool layer already enforces, or keep them as guidance. Either is fine. The
  real limits are the admin checks, the shell permissions and the safety filter.

Per-user rules move from `personality_overrides.json` to `bot.overrides_file` (see step 2).

## Cutover

Keep the old setup running until step 6. Steps 1 to 3 change nothing the old bot reads.

### 1. Install the new code and dependencies

```bash
REPO=$HOME/path/to/magbot                  # the rewrite checkout
sudo mkdir -p /opt/magbot && sudo chown "$USER" /opt/magbot
cp -r "$REPO/magbot" "$REPO/webui" "$REPO/persona.md" "$REPO/pyproject.toml" "$REPO/requirements.txt" /opt/magbot/
python3 -m venv /opt/magbot/venv
/opt/magbot/venv/bin/pip install -r /opt/magbot/requirements.txt
mkdir -p /opt/magbot/data /opt/magbot/www
```

### 2. Write the configuration and secrets

```bash
cp "$REPO/config.example.toml" /opt/magbot/config.toml
cp "$REPO/.env.example" /opt/magbot/.env && chmod 600 /opt/magbot/.env
```

Edit `/opt/magbot/config.toml`:

- `bot.admins`, `bot.shell_users`, `bot.names` from the old `.env`
- `bot.file_base_url` from the old `FILE_BASE_URL`
- `llm.timeout = 300.0` (see above)
- the `[[llm.providers]]` block from the Hermes profile
- `[consent]` from `STOAT_OPTIN*` and `STOAT_NO_CONTEXT*`
- `bot.overrides_file = "personality_overrides.json"` if you have one (step 4)

Edit `/opt/magbot/.env`:

- `STOAT_BOT_TOKEN` from the old `.env`
- the key variable named by the provider's `api_key_env`, for example `CLINE_API_KEY`
- `BRAVE_API_KEY` or `IMAGE_API_KEY` only if you use them

Check that the configuration loads, without starting the bot:

```bash
cd /opt/magbot && MAGBOT_CONFIG=/opt/magbot/config.toml \
  /opt/magbot/venv/bin/python -c "from magbot import config; c = config.load(); print(c.bot.data_dir, [p.name for p in c.llm.providers])"
```

An unknown setting name fails here with a message that names it.

### 3. Copy the consent lists

Use the old files as they are. The formats are the same.

```bash
OLD=/opt/stoat-bot
cp "$OLD/optin.list" "$OLD/optout.list" /opt/magbot/data/
cp "$OLD/optin-prompted.json" /opt/magbot/data/ 2>/dev/null || true
```

If the old `.env` set `OPT_IN_FILE`, `OPT_OUT_FILE` or `OPTIN_PROMPT_STATE`, copy from those paths
instead.

### 4. Copy the web folder and personality overrides

```bash
cp -a "$OLD/www/." /opt/magbot/www/
cp "$OLD/personality_overrides.json" /opt/magbot/ 2>/dev/null || true
```

Write `/opt/magbot/persona.md` now, as described above, and check it renders. Use the placeholders.

### 5. Install the units

```bash
cp "$REPO/deploy/magbot.service" "$REPO/deploy/magbot-files.service" ~/.config/systemd/user/
systemctl --user daemon-reload

# Web UI (system unit). Keep a copy of the old unit so you can roll back.
sudo cp /etc/systemd/system/stoat-webui.service /etc/systemd/system/stoat-webui.service.old
sudo cp "$REPO/deploy/stoat-webui.service" /etc/systemd/system/
sudo systemctl daemon-reload
```

Do not start the new units yet. Leave the old `stoat-bot*` units in `~/.config/systemd/user/`, because
rollback needs them.

### 6. Switch over

Only one process may hold the bot token at a time. If both run, both answer.

```bash
# Stop the old bot and its file server, and keep them from coming back at login.
systemctl --user stop stoat-bot stoat-bot-http
systemctl --user disable stoat-bot stoat-bot-http

# Copy the consent lists again: they may have changed since step 3.
cp "$OLD/optin.list" "$OLD/optout.list" /opt/magbot/data/
cp "$OLD/optin-prompted.json" /opt/magbot/data/ 2>/dev/null || true

# Start the new bot and its file server.
systemctl --user enable --now magbot magbot-files

# The web UI now reads /opt/magbot/.env and runs from /opt/magbot.
sudo systemctl restart stoat-webui
```

### 7. Check it

```bash
systemctl --user status magbot magbot-files
journalctl --user -u magbot -n 100 --no-pager
systemctl status stoat-webui
```

In the log, look for:

- a ready line that names the bot, its servers, and the consent counts (allowed ids, allowed tags,
  excluded)
- no traceback and no `unknown ... setting` error

Then test from an opted-in account:

- ping the bot and check it replies
- send `/help` and check the command list
- ask it something that needs the web (a search) to check the tool loop
- write a file to the public folder and open its URL through the proxy

The file server now listens on `127.0.0.1:8080` only. Point the reverse proxy at `127.0.0.1:8080`. If it
pointed at the server's public address, it must change, or the file URLs will stop working.

### Rollback

If something is wrong, go back to the old setup. Remember that opt-ins and opt-outs made while the new
bot ran are only in `/opt/magbot/data`.

```bash
systemctl --user stop magbot magbot-files
systemctl --user enable --now stoat-bot stoat-bot-http

sudo cp /etc/systemd/system/stoat-webui.service.old /etc/systemd/system/stoat-webui.service
sudo systemctl daemon-reload && sudo systemctl restart stoat-webui

# Only if the new bot changed consent state you want to keep:
cp /opt/magbot/data/optin.list /opt/magbot/data/optout.list "$OLD/"
```

Point the proxy back at the old file server port if you changed it.

## After cutover

- Leave the Hermes profile and `/opt/stoat-bot` alone for a week or so. Archive them when you are sure.
- Remove the old units (`stoat-bot`, `stoat-bot-http`) from `~/.config/systemd/user/` when you no
  longer need the rollback.
- The new memory starts empty. Saved facts from the old bot are not available. Users who want them
  back can tell the bot again.
- Run `loginctl enable-linger "$USER"` once if the user units stop when you log out. The old units
  needed it too.

Memories imported with `scripts/import_hermes_memory.py` for a user whose Stoat id is not known are
kept under their `name#1234` tag, if that user is opted in. They attach to the account automatically
the next time that user talks to the bot. Until then they are not used.
