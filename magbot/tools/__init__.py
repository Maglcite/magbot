"""Tools the model can call.

Adding a tool: create a module in this package (or add to an existing one)
and decorate an async function with @tool. It receives a ToolContext plus the
model's arguments as keyword arguments, and returns a string for the model.
Raise ToolError for a clean, user-facing failure. Then import the module at
the bottom of this file.

    @tool(
        "dice",
        "Roll an N-sided die.",
        {"type": "object", "properties": {"sides": {"type": "integer"}}, "required": ["sides"]},
    )
    async def dice(ctx: ToolContext, sides: int) -> str:
        return str(random.randint(1, sides))

Pass `allowed=` to restrict who may use a tool (it is then hidden from the
model for everyone else).
"""
from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..config import Config
from ..identity import User

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    config: Config
    user: User
    is_admin: bool
    can_shell: bool
    channel_id: str
    memory: object          # magbot.memory.MemoryStore (untyped to avoid an import cycle)
    # Files a tool produced during this reply (paths inside www_dir), if any.
    outputs: list[str] = field(default_factory=list)


class ToolError(Exception):
    """A failure whose message is safe to show the model (and the user)."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., Awaitable[str]]
    allowed: Callable[[ToolContext], bool] | None = None

    def is_available(self, ctx: ToolContext) -> bool:
        if self.name in ctx.config.tools.disabled:
            return False
        return self.allowed is None or bool(self.allowed(ctx))

    def schema(self) -> dict:
        """OpenAI chat-completions tool definition."""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict,
         allowed: Callable[[ToolContext], bool] | None = None):
    def register(func):
        if name in REGISTRY:
            raise ValueError(f"tool {name!r} registered twice")
        REGISTRY[name] = Tool(name, description, parameters, func, allowed)
        return func
    return register


def available(ctx: ToolContext) -> list[Tool]:
    return [t for t in REGISTRY.values() if t.is_available(ctx)]


async def call(name: str, arguments: str | dict, ctx: ToolContext) -> str:
    """Run a tool call from the model. Never raises: errors become the result."""
    t = REGISTRY.get(name)
    if t is None or not t.is_available(ctx):
        return f"error: tool {name!r} is not available"
    try:
        if arguments is None or arguments == "":
            args = {}
        elif isinstance(arguments, str):
            args = json.loads(arguments)
        else:
            args = dict(arguments)
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (ValueError, TypeError, RecursionError) as e:
        return f"error: bad arguments for {name}: {e}"
    # Models often send null for an optional argument: treat it as not given.
    args = {k: v for k, v in args.items() if v is not None}
    try:
        inspect.signature(t.func).bind(ctx, **args)
    except TypeError as e:
        return f"error: bad arguments for {name}: {e}"
    log.info("tool %s by %s: %s", name, ctx.user.tag, str(args)[:200])
    try:
        return str(await t.func(ctx, **args))
    except ToolError as e:
        return f"error: {e}"
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"error: {name} failed ({type(e).__name__})"


# Built-in tools register themselves on import.
from . import files, images, memory, shell, web  # noqa: E402,F401
