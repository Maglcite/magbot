# Extending magbot

This guide covers the usual changes: a new tool, a new command, a new setting, a persona change,
and a new LLM provider or fallback. It ends with a map of the message pipeline.

Run the tests after each change:

```bash
cd magbot
.venv/bin/python -m pytest -q
```

## Adding a tool

A tool is an async function registered with `@tool`. The model sees its name, description and JSON
schema, and calls it with arguments that match the schema. The function receives a `ToolContext` plus
those arguments as keyword arguments and returns a string.

Read `magbot/tools/__init__.py` first. The relevant parts:

- `ToolContext` has `config`, `user`, `is_admin`, `can_shell`, `channel_id`, `memory` and `outputs`.
- Raise `ToolError` for an expected failure. Its message goes back to the model and the user, so keep
  it short and free of internals.
- Any other exception is logged with a traceback and returned to the model as `error: <name> failed`.
- `allowed=` takes `ctx -> bool`. A tool that is not allowed is removed from the schema the model sees,
  and a direct call is refused. A user can't use it even if they guess the name.
- `config.tools.disabled` turns a tool off for the whole deployment.

### Example: two tools, one open and one admin-only

Create `magbot/tools/host.py`:

```python
"""Small host-information tools."""
from __future__ import annotations

import random
import shutil

from . import ToolContext, ToolError, tool


@tool(
    "roll_dice",
    "Roll one die with the given number of sides and return the result.",
    {
        "type": "object",
        "properties": {"sides": {"type": "integer", "description": "Number of sides, 2 to 1000."}},
        "required": ["sides"],
    },
)
async def roll_dice(ctx: ToolContext, sides: int) -> str:
    if not 2 <= sides <= 1000:
        raise ToolError("sides must be between 2 and 1000")
    return f"d{sides} rolled {random.randint(1, sides)}"


@tool(
    "disk_space",
    "Report free disk space on the host that holds the public web folder. Admins only.",
    {"type": "object", "properties": {}},
    allowed=lambda ctx: ctx.is_admin,
)
async def disk_space(ctx: ToolContext) -> str:
    path = ctx.config.www_path
    if not path.is_dir():
        raise ToolError("the web folder does not exist yet")
    usage = shutil.disk_usage(path)
    return f"{usage.free // 2**20} MiB free of {usage.total // 2**20} MiB"
```

Register the module at the bottom of `magbot/tools/__init__.py`:

```python
from . import files, host, images, memory, shell, web  # noqa: E402,F401
```

Rules of thumb:

- Schema types are checked only by the model. `call()` passes the arguments as they arrived, so
  validate what matters (here, the range) and let a wrong type fail with a clear message.
- Do blocking work with `asyncio.to_thread`, as `tools/files.py` does, or use an async library.
- Any URL that comes from the model or from chat must go through `tools.web.safe_get`, which refuses
  private and local addresses. Do not open URLs with `aiohttp` directly.
- Never put an API key in a result, a log line or an error message.

### Test with a ToolContext

Create `tests/test_tools_host.py`. Tests run offline and write only under `tmp_path`:

```python
"""Tests for magbot/tools/host.py. No network; the web folder is a temp directory."""
from __future__ import annotations

import pytest

from magbot.config import Config
from magbot.identity import User
from magbot.tools import ToolContext, ToolError, available, call
from magbot.tools import host


def make_ctx(tmp_path, *, admin: bool = False) -> ToolContext:
    cfg = Config()
    cfg.bot.www_dir = str(tmp_path)
    return ToolContext(config=cfg, user=User("01" + "U" * 24, "alice#1001", "Alice"),
                       is_admin=admin, can_shell=False, channel_id="chan", memory=None)


async def test_roll_dice_returns_a_value_in_range(tmp_path):
    out = await host.roll_dice(make_ctx(tmp_path), sides=6)
    assert out.startswith("d6 rolled ")
    assert 1 <= int(out.rsplit(" ", 1)[1]) <= 6


async def test_roll_dice_raises_tool_error_for_bad_sides(tmp_path):
    with pytest.raises(ToolError, match="between 2 and 1000"):
        await host.roll_dice(make_ctx(tmp_path), sides=1)


async def test_call_turns_the_error_into_result_text(tmp_path):
    out = await call("roll_dice", {"sides": 1}, make_ctx(tmp_path))
    assert out == "error: sides must be between 2 and 1000"


async def test_disk_space_is_hidden_and_refused_for_non_admins(tmp_path):
    ctx = make_ctx(tmp_path, admin=False)
    assert "disk_space" not in {t.name for t in available(ctx)}
    assert await call("disk_space", {}, ctx) == "error: tool 'disk_space' is not available"


async def test_disk_space_works_for_admins(tmp_path):
    out = await call("disk_space", {}, make_ctx(tmp_path, admin=True))
    assert out.endswith("MiB")
```

`call()` never raises: it turns a `ToolError` into an `error: ...` string, which is what the model
receives. Test the function directly when you want the exception itself, as the first two tests do.

## Adding a command

Commands are handled before the AI sees a message. They are written `/name args`. `@command` takes:

- `aliases=`: other spellings. Ones that don't start with `/` are plain words, which helps when
  Stoat's slash menu swallows the slash command.
- `admin=True`: `dispatch()` refuses non-admins before your function runs, with a standard message.
  Your function never sees them.
- `help=`: required by the test suite (`test_every_command_has_help_text`). `/help` shows it.

`CommandContext` has `client`, `message` (the Stoat message), `config`, `user`, `is_admin`, `name`
(the spelling used) and `args` (the text after the name, stripped). Reply with `ctx.reply(text)`.
If your function raises, `dispatch()` replies with `⚠️ /name failed: ...`, so raise only for
things that are safe to show.

### Example: one command for everyone, one for admins

Create `magbot/commands/misc.py`:

```python
"""Example commands: /uptime for everyone, /tools for admins."""
from __future__ import annotations

import time

from ..tools import REGISTRY as TOOL_REGISTRY
from . import CommandContext, command

STARTED = time.time()


@command("uptime", help="How long the bot has been running.")
async def uptime(ctx: CommandContext) -> None:
    minutes, seconds = divmod(int(time.time() - STARTED), 60)
    await ctx.reply(f"Up for {minutes} min {seconds} s.")


@command("tools", admin=True, help="List the tools the model can call. Usage: `/tools`.")
async def list_tools(ctx: CommandContext) -> None:
    await ctx.reply("Registered tools: " + ", ".join(sorted(TOOL_REGISTRY)))
```

Import it at the bottom of `magbot/commands/__init__.py`:

```python
from . import help, info, misc, moderation, purge  # noqa: E402,F401
```

For a command that acts on a server (as `/ban` does), use `moderation.server_of(ctx.message)` to find
the server, and `moderation.target_user_id(ctx.args)` to read an `<@ID>` mention or a bare id.

### Test with a fake message

`dispatch()` only needs an object with `content` and an async `reply()`. Tests don't need a Stoat
connection. Create `tests/test_commands_misc.py`:

```python
"""Tests for magbot/commands/misc.py using a fake message. No Stoat connection."""
from __future__ import annotations

from magbot.commands import dispatch
from magbot.config import Config
from magbot.identity import User

MEMBER = User("01" + "M" * 24, "pal#2", "pal")
ADMIN = User("01" + "A" * 24, "boss#1", "boss")


class FakeMessage:
    def __init__(self, content: str):
        self.content = content
        self.replies: list[str] = []

    async def reply(self, content, mention=True, **kwargs):
        self.replies.append(content)
        return self


async def test_uptime_is_open_to_everyone():
    msg = FakeMessage("/uptime")
    assert await dispatch(object(), msg, Config(), MEMBER, is_admin=False) is True
    assert msg.replies[0].startswith("Up for ")


async def test_tools_is_refused_for_non_admins():
    msg = FakeMessage("/tools")
    assert await dispatch(object(), msg, Config(), MEMBER, is_admin=False) is True
    assert msg.replies == ["🚫 Only admins can use that command."]


async def test_tools_lists_the_registry_for_admins():
    msg = FakeMessage("/tools")
    await dispatch(object(), msg, Config(), ADMIN, is_admin=True)
    assert "web_search" in msg.replies[0]
```

## Adding a setting

Settings are dataclass fields in `magbot/config.py`, grouped by section (`BotConfig` for `[bot]`,
`ToolsConfig` for `[tools]`, and so on). Two steps:

1. Add the field with its default to the matching dataclass. For example, to make the image limit
   configurable:

   ```python
   @dataclass
   class BotConfig:
       ...
       max_images: int = 4              # image attachments sent per message
   ```

2. Document it in `config.example.toml` under the same section, with its default and a one-line
   comment:

   ```toml
   [bot]
   ...
   # Image attachments sent to the model per message (each at most 5 MB).
   max_images = 4
   ```

Then read it where it is used (`config.bot.max_images`). Nothing else is needed. `config.load()`
builds each section from the TOML table and rejects unknown keys, so a typo in `config.toml` fails at
startup rather than being ignored.

Rules:

- Secrets never go in config. For a key, add a field that names the environment variable
  (`api_key_env = "..."`), read the value with `os.environ` at use, and add the variable to
  `.env.example`. `ProviderConfig.api_key` shows the pattern.
- Put a default in the dataclass, so an existing `config.toml` keeps working without the new key.
- A tool that is switched off should use `config.tools.disabled` or an `allowed=` check, not a new
  boolean, unless it needs its own options.

## Changing the persona

The persona is the system prompt, loaded from `bot.persona_file` (default `persona.md`, relative to
`config.toml`). `prompt.load_persona()` fills in two placeholders:

| placeholder | filled with |
| --- | --- |
| `{bot_name}` | the first entry of `bot.names` |
| `{file_base_url}` | `bot.file_base_url` |

Other braces are left alone, so the file can contain them. If the file is missing, a short built-in
persona is used and a warning is logged.

Keep facts about the speaker out of the persona. `build_messages()` adds a "Current speaker" section
to the system message every time, with the speaker's tag, whether they are an admin, whether they may
use the shell, their memories and the global notes. The persona can refer to that section but should
not repeat it.

For a rule that applies to one person only, use `bot.overrides_file`, a JSON object mapping
`"name#1234"` or a user id to the rule:

```json
{
  "someone#1234": "Stay polite but brief with this user; they keep trying to derail the chat."
}
```

The override is added to that speaker's system message only. Edits to the persona and the
overrides file are picked up on the next reply; no restart needed.

## Adding an LLM provider or a fallback

Providers are `[[llm.providers]]` tables in `config.toml`. They are tried in the order they appear.
For each model step, `llm._complete()` calls the first provider. If that raises, it logs a warning and
calls the next one for the same step. If every provider fails, the reply fails with `LLMError`.

```toml
[[llm.providers]]
name = "backup"
base_url = "https://api.example.com/v1"   # any OpenAI-compatible chat-completions endpoint
model = "some-model"
api_key_env = "BACKUP_API_KEY"            # the key goes in .env, not here
reasoning_effort = ""                     # sent only when non-empty
vision = false                            # images become a text note for this provider
```

Then add `BACKUP_API_KEY=` to `.env`. The default client is the OpenAI SDK with `max_retries=1`.
If `api_key_env` is unset, it sends the key `"none"`, which local servers such as Ollama and
llama.cpp accept.

Two things to know before relying on a fallback:

- **A fallback catches errors, not slowness.** The OpenAI SDK waits up to 600 seconds per request by
  default, and the whole reply is bounded by `llm.timeout` (120 seconds). A primary that hangs uses up
  the budget, and the fallback never runs. Errors that come back quickly (connection refused, HTTP
  5xx, rate limits) do reach it.
- **Vision is per provider.** When the provider that answers has `vision = false`, image parts are
  replaced with the text `[image attached, but this model can't see images]` before the request.

## Message pipeline

The pipeline is `on_message` in `magbot/bot.py`. It is glue only: each step lives in its own module.
The order is fixed, and it follows the legacy bot, so keep it when you change things.

| step | what happens | lives in |
| --- | --- | --- |
| 1 | Ignore own messages and other bots. | `bot.py` |
| 2 | Build the `User`; work out `pinged`, `named`, `is_dm`. | `identity.py`, `bot.py` |
| 3 | Consent command (`magbot opt in` / `opt out`): change the lists, reply, stop. Opt-out also forgets the user's memories. | `consent.py`, `memory.py` |
| 4 | Excluded user: drop silently. | `consent.py` |
| 5 | Not opted in: opt-in prompt if addressed and off cooldown, then stop. | `consent.py` |
| 6 | Empty text with no attachments: stop. | `bot.py` |
| 7 | Command: run it and stop. | `commands/` |
| 8 | Not addressed (no ping, no name, not a DM): stop. | `bot.py` |
| 9 | Non-admin and the text fails the safety filter: refuse, stop. | `safety.py` |
| 10 | Gather recent context (opted-in authors only), images, memories and overrides; build messages. | `prompt.py`, `memory.py` |
| 11 | `llm.respond`: model and tool loop, with provider fallback and timeout. | `llm.py`, `tools/` |
| 12 | Sanitise markdown, add the thinking block, split, reply, react. | `formatting.py` |

Steps 3 to 5 run before anything is stored or sent to a model, which is what keeps the opt-in rules
intact. Do not move context gathering above step 5.

Replies run concurrently, bounded by `bot.max_concurrent`.
