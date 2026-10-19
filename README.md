# magbot

magbot is an AI chat bot for [Stoat.chat](https://stoat.chat). It talks to any OpenAI-compatible
chat-completions API directly and runs its own small tool-calling loop. There is no separate agent
process. The bot answers when someone pings it or says its name, can search the web, read pages,
publish files, remember facts, and moderate the server for admins. It only collects messages from
users who have opted in.

Requires Python 3.11 or newer.

## Features

- **Triggers.** It replies to an @-ping or to any configured name (`magbot`, `mag bot`, `mag-bot`,
  `fembot` by default). In a DM every message counts as addressed to it; group channels follow the
  server rule and need the ping or name.
- **Channel context.** The last few messages in the channel are passed along, but only from users
  who have opted in. Pings and names are stripped from them. If a message is a reply, the message it
  replies to is passed too, when its author has opted in.
- **Rate limit.** Each user gets `bot.rate_limit_per_minute` AI replies per minute (6 by default).
  Admins are exempt. Over the limit the bot reacts with ⏳ and does not call the model. 0 turns it off.
- **Tools.** The model can call the tools below. Tools a user may not use are hidden from the model.
- **Long-term memory.** Facts are kept in a local SQLite database, per user, plus global notes that
  only admins can write. Users can see and delete their own; admins can delete any.
- **Opt-in consent.** Nothing is collected or answered until a user opts in. Opting out also deletes
  their saved memories. See [docs/CONSENT.md](docs/CONSENT.md).
- **Admin moderation.** `/ban`, `/kick` and `/unban`, plus `/del-all` (alias `/purge`) to delete one
  user's messages in a channel. Purges are a dry run first and need a confirm.
- **Thinking traces.** Models that expose reasoning can show it as a quoted or spoiler block, capped in
  length. Off by default.
- **Image understanding.** Image attachments are sent to the model when its provider has `vision = true`.
  Otherwise the model gets a text note saying an image was attached.
- **Web UI.** A password-protected browser client for the bot account, in `webui/`. See
  [docs/WEBUI.md](docs/WEBUI.md).

### Commands

| command | aliases | who | what it does |
| --- | --- | --- | --- |
| `/help` | | everyone | Lists the commands you can use. |
| `/ban @user [reason]` | `banbanbanban` | admins | Bans the user from the server. |
| `/kick @user` | | admins | Kicks the user from the server. |
| `/unban @user` | | admins | Lifts a server ban. Accepts a pasted user ID. |
| `/del-all @user [confirm]` | `/delall`, `/del_all`, `/purge`, `delalldelalldelall`, `purgepurgepurge` | admins | Dry run first, then `confirm` within 5 minutes to delete. Old messages are deleted one at a time. |

The word forms (`banbanbanban`, `purgepurgepurge`) exist because Stoat's slash menu sometimes
swallows slash commands.

### Tools

| tool | who | what it does |
| --- | --- | --- |
| `web_search` | everyone | Search the web. Backend: DuckDuckGo (default), SearXNG or Brave. |
| `fetch_url` | everyone | Read an http(s) page as text. Refuses private and local addresses, including on redirects. |
| `write_file` | everyone | Save text into the public web folder and return its URL. |
| `read_file` | everyone | Read a text file from the web folder. |
| `list_files` | everyone | List files in the web folder with sizes and URLs. |
| `delete_file` | admins | Delete a file from the web folder. |
| `run_shell` | `shell_users` only | Run a shell command in the web folder, with a timeout and output cap. Non-admins are refused for commands the safety filter flags. |
| `generate_image` | everyone, when `tools.image_base_url` is set | Generate an image and publish it in the web folder. |
| `remember` | everyone (`global` scope: admins) | Save a fact about the speaker, or a note about everyone. |
| `forget` | everyone (own memories), admins (any) | Delete a saved memory by its id. |

Any tool can be switched off with `tools.disabled`.

## Architecture

Modules under `magbot/`:

```
magbot/
  __main__.py        python -m magbot: load config, set up logging, run the bot
  bot.py             MagBot (stoat.Client): the on_message pipeline, glue only
  config.py          settings dataclasses and load() from config.toml
  identity.py        User, admin and shell checks
  consent.py         opt-in store and consent command parsing
  memory.py          SQLite long-term memory
  prompt.py          persona loading and chat message assembly
  llm.py             OpenAI-compatible client, provider fallback, tool loop
  formatting.py      markdown fixes, thinking blocks, message splitting
  safety.py          dangerous-request filter for non-admins
  tools/             @tool registry and the built-in tools
  commands/          @command registry and the built-in commands
```

Message pipeline (`bot.py`). The order matters, and it is the same order the legacy bot used:

```
Stoat message event
  |
  1  ignore own messages and other bots
  2  identify the author; work out pinged / named / DM
  3  consent command ("magbot opt in" / "magbot opt out")?  -> update lists, reply, stop
  4  excluded (opted out or always_exclude)?               -> drop silently, stop
  5  not opted in?  -> opt-in prompt if pinged/named and off cooldown, stop
  6  empty text with no attachments?                       -> stop
  7  command (/ban, /purge, /help, ...)?                   -> run it, stop
  8  not pinged, not named, not a DM?                      -> stop
  9  non-admin and text fails the safety filter?          -> refusal, stop
 10  gather: context (opted-in authors only), images,
     memories, global notes, per-user override
     -> prompt.build_messages
 11  llm.respond: model <-> tools loop, provider fallback, timeout
 12  sanitize markdown, add thinking block, split, reply in chunks; react
```

Replies run concurrently, bounded by `bot.max_concurrent`.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml   # edit: admins, names, file_base_url, providers
cp .env.example .env                 # fill in STOAT_BOT_TOKEN and OPENAI_API_KEY
chmod 600 .env

set -a; . ./.env; set +a             # export the variables for a manual run
export MAGBOT_CONFIG=./config.toml
python -m magbot
```

`persona.md` is the default system prompt. Edit it in place or point `bot.persona_file` at another file.
Its `{bot_name}` and `{file_base_url}` placeholders are filled in. The persona and the overrides file
are re-read when they change, so edits apply to the next message without a restart.

The defaults for `bot.data_dir` (`/opt/magbot/data`) and `bot.www_dir` (`/opt/magbot/www`) are for the
systemd install. For a manual run, set both in `config.toml` to directories you own, such as `./data`
and `./www`. They are created when needed.

## Install with systemd

Deployment layout: code, venv, config and secrets all under `/opt/magbot`. Run as your normal user.

```bash
sudo mkdir -p /opt/magbot && sudo chown "$USER" /opt/magbot
cp -r magbot webui persona.md pyproject.toml requirements.txt /opt/magbot/
python3 -m venv /opt/magbot/venv
/opt/magbot/venv/bin/pip install -r /opt/magbot/requirements.txt

cp config.example.toml /opt/magbot/config.toml
cp .env.example /opt/magbot/.env && chmod 600 /opt/magbot/.env
mkdir -p /opt/magbot/data /opt/magbot/www

mkdir -p ~/.config/systemd/user
cp deploy/magbot.service deploy/magbot-files.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now magbot magbot-files
loginctl enable-linger "$USER"        # keep the user services running after logout
```

- `magbot.service` runs `python -m magbot` with `MAGBOT_CONFIG=/opt/magbot/config.toml` and
  `EnvironmentFile=/opt/magbot/.env`. It restarts on failure.
- `magbot-files.service` serves `www_dir` on `127.0.0.1:8080`. Put a TLS reverse proxy in front of it
  and serve it at `bot.file_base_url`. Do not expose the port directly.
- `deploy/stoat-webui.service` is the optional web UI. It is a system unit, so install it with `sudo`
  into `/etc/systemd/system/`.

Logs: `journalctl --user -u magbot -f`.

Migrating from the old Hermes-based bot: see [docs/MIGRATION.md](docs/MIGRATION.md).

## Configuration

Settings live in `config.toml`, and `config.example.toml` lists every one of them with its default and
a comment. Secrets stay out of the file: the bot token is `STOAT_BOT_TOKEN`, and each provider or
service names the environment variable that holds its key (`api_key_env`, `brave_api_key_env`,
`image_api_key_env`). The variables are documented in `.env.example`.

Sections:

| section | what it holds |
| --- | --- |
| `[bot]` | names, admins, shell users, data and web folders, persona, context size, per-user rate limit, thinking display |
| `[[llm.providers]]` | one table per provider, tried in order: base URL, model, key variable, vision, reasoning effort |
| `[llm]` | reply timeout, tool steps, token limit, temperature |
| `[tools]` | disabled tools, search backend, shell limits, image endpoint |
| `[consent]` | opt-in prompt cooldown, `always_allow`, `always_exclude` |

## Privacy

magbot does not collect messages from users who have not opted in. Those messages are not stored,
not used as context, and not sent to the model. If such a user pings the bot or says its name, the bot
sends a short explanation of how to opt in, at most once per cooldown (12 hours by default). Opting in
and out is done by naming the bot: `magbot opt in` or `magbot opt out`.

When an opted-in user messages the bot, the message, the few recent messages around it (from opted-in
users only), the message it replies to (only if its author is opted in), any images attached to it, and
their saved memories are sent to the configured model provider. Anything the model writes into `www_dir` is public at `file_base_url`. Opting out deletes the
user's saved memories.

Operators can exclude people silently with `consent.always_exclude` or `optout.list`. Anyone with
shell access to the host can read `data_dir`, which holds the consent lists and `memory.db`. The full
policy and file formats are in [docs/CONSENT.md](docs/CONSENT.md).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests run offline. They use no Stoat connection and no LLM, and they write only to temporary
directories.

## Extending

Tools and commands are plain functions registered with a decorator:

```python
# magbot/tools/host.py, then import it at the bottom of magbot/tools/__init__.py
@tool("roll_dice", "Roll one die.",
      {"type": "object", "properties": {"sides": {"type": "integer"}}, "required": ["sides"]})
async def roll_dice(ctx: ToolContext, sides: int) -> str:
    if not 2 <= sides <= 1000:
        raise ToolError("sides must be between 2 and 1000")
    return str(random.randint(1, sides))
```

```python
# magbot/commands/misc.py, then import it at the bottom of magbot/commands/__init__.py
@command("uptime", help="How long the bot has been running.")
async def uptime(ctx: CommandContext) -> None:
    await ctx.reply(f"Up for {int(time.time() - STARTED)} seconds.")
```

See [docs/EXTENDING.md](docs/EXTENDING.md) for complete examples with permissions (`allowed=`,
`admin=True`), tests, new settings, persona changes and new providers.
