"""Entry point: `python -m magbot [--config PATH]`."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import config as config_module
from .bot import MagBot

log = logging.getLogger(__name__)


def _log_level() -> int:
    """$MAGBOT_LOG_LEVEL as a logging level; INFO when unset or not a level name."""
    name = os.environ.get("MAGBOT_LOG_LEVEL", "").strip().upper() or "INFO"
    level = logging.getLevelNamesMapping().get(name)
    if level is None:
        print(f"magbot: unknown MAGBOT_LOG_LEVEL {name!r}, using INFO", file=sys.stderr)
        return logging.INFO
    return level


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="magbot", description="AI chat bot for Stoat.chat.")
    parser.add_argument("--config", metavar="PATH",
                        help="settings file (default: $MAGBOT_CONFIG or ./config.toml)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=_log_level(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        config = config_module.load(args.config)
    except (OSError, ValueError) as e:
        print(f"magbot: could not load the config: {e}", file=sys.stderr)
        return 2

    if not config.token:
        print("magbot: no bot token. Set STOAT_BOT_TOKEN in the environment.", file=sys.stderr)
        return 2
    if not any(p.api_key for p in config.llm.providers):
        # Local providers may not need a key, so this is only a warning.
        env_names = ", ".join(p.api_key_env for p in config.llm.providers) or "(none)"
        log.warning("no LLM provider has an API key set (expected one of: %s); "
                    "replies will fail unless a provider works without one", env_names)

    try:
        bot = MagBot(config)  # creates data_dir / www_dir and loads the persona
    except OSError as e:
        print(f"magbot: could not set up data or www directories: {e}", file=sys.stderr)
        return 2
    try:
        bot.run(config.token)
    finally:
        bot.memory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
