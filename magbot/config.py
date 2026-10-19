"""Settings, loaded once at startup from a TOML file plus a few env vars.

Non-secret settings live in config.toml (path from $MAGBOT_CONFIG, default
./config.toml). Secrets stay in the environment: the bot token is
$STOAT_BOT_TOKEN, and every API key is referenced by the *name* of its env var
(e.g. api_key_env = "CLINE_API_KEY") so the TOML file is safe to share.

Adding a setting: give it a default in the matching dataclass below; it can
then be set in config.toml under the same section and name.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class BotConfig:
    # Words that count as addressing the bot (matched case-insensitively).
    names: list[str] = field(default_factory=lambda: ["magbot", "mag bot", "mag-bot", "fembot"])
    # Exact "name#1234" tags or Stoat user ids.
    admins: list[str] = field(default_factory=list)
    shell_users: list[str] = field(default_factory=list)
    data_dir: str = "/opt/magbot/data"          # consent lists + memory.db
    www_dir: str = "/opt/magbot/www"            # files served publicly
    file_base_url: str = "https://example.com"  # public URL of www_dir
    persona_file: str = "persona.md"
    overrides_file: str = ""                    # optional JSON: {"name#1234": "extra rule"}
    recent_context: int = 8                     # earlier channel messages passed as context
    max_concurrent: int = 4                     # replies generated at the same time
    max_message_chars: int = 1600               # longer replies are split
    thinking_mode: str = "off"                  # off | blockquote | spoiler | plain
    thinking_max_lines: int = 15
    thinking_max_chars: int = 1200


@dataclass
class ProviderConfig:
    name: str = "default"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = "OPENAI_API_KEY"
    reasoning_effort: str = ""      # sent only when set (e.g. "low")
    vision: bool = False            # model accepts image inputs
    headers: dict[str, str] = field(default_factory=dict)   # extra HTTP headers, if a gateway needs them

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class LLMConfig:
    providers: list[ProviderConfig] = field(default_factory=lambda: [ProviderConfig()])
    timeout: float = 120.0          # seconds for one whole reply, tools included
    request_timeout: float = 45.0   # seconds per model request; a hung provider then falls back
    max_tool_steps: int = 8         # model<->tool round trips per reply
    max_tokens: int = 3500
    temperature: float | None = None


@dataclass
class ToolsConfig:
    disabled: list[str] = field(default_factory=list)   # tool names to turn off
    search_backend: str = "duckduckgo"                   # duckduckgo | searxng | brave
    searxng_url: str = ""
    brave_api_key_env: str = "BRAVE_API_KEY"
    fetch_max_chars: int = 8000
    shell_timeout: float = 30.0
    shell_max_output: int = 3000
    # OpenAI-compatible /images/generations endpoint; the image tool is off
    # while image_base_url is empty.
    image_base_url: str = ""
    image_model: str = ""
    image_api_key_env: str = "IMAGE_API_KEY"


@dataclass
class ConsentConfig:
    prompt_cooldown: int = 43200            # seconds between opt-in prompts per user
    always_allow: list[str] = field(default_factory=list)    # tags/ids treated as opted in
    always_exclude: list[str] = field(default_factory=list)  # tags/ids never collected


@dataclass
class Config:
    bot: BotConfig = field(default_factory=BotConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    consent: ConsentConfig = field(default_factory=ConsentConfig)
    token: str = ""                 # from $STOAT_BOT_TOKEN
    self_id: str = ""               # the bot's own user id, filled in on_ready

    @property
    def data_path(self) -> Path:
        return Path(self.bot.data_dir)

    @property
    def www_path(self) -> Path:
        return Path(self.bot.www_dir)


def _build(cls, raw: dict):
    """Instantiate dataclass `cls` from a dict, rejecting unknown keys."""
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} setting(s): {', '.join(sorted(unknown))}")
    return cls(**raw)


def load(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("MAGBOT_CONFIG", "config.toml"))
    if path.exists():
        raw = tomllib.loads(path.read_text())
    else:
        log.warning("config file %s not found; using built-in defaults", path)
        raw = {}
    unknown = set(raw) - {"bot", "llm", "tools", "consent"}
    if unknown:
        raise ValueError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    llm_raw = dict(raw.get("llm", {}))
    providers = [_build(ProviderConfig, p) for p in llm_raw.pop("providers", [])]
    llm = _build(LLMConfig, llm_raw)
    if providers:
        llm.providers = providers
    cfg = Config(
        bot=_build(BotConfig, raw.get("bot", {})),
        llm=llm,
        tools=_build(ToolsConfig, raw.get("tools", {})),
        consent=_build(ConsentConfig, raw.get("consent", {})),
        token=os.environ.get("STOAT_BOT_TOKEN", ""),
    )
    # Relative paths in the config are relative to the config file.
    base = path.resolve().parent
    for attr in ("persona_file", "overrides_file"):
        value = getattr(cfg.bot, attr)
        if value and not os.path.isabs(value):
            setattr(cfg.bot, attr, str(base / value))
    return cfg
