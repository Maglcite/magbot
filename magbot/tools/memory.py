"""Memory tools: let the model save and forget notes about users and the server."""
from __future__ import annotations

import logging

from . import ToolContext, ToolError, tool

log = logging.getLogger(__name__)

# Same value as magbot.memory.MemoryStore.GLOBAL; kept local so this module
# doesn't depend on the store's module.
GLOBAL_SCOPE = "*"
MAX_FACT_CHARS = 500


@tool(
    "remember",
    "Save a fact to long-term memory so it can be recalled in later conversations. Use scope "
    "'user' (the default) for facts about the person you are talking to. Use scope 'global' only "
    "for notes about everyone or the server; only admins may save global notes. Returns the id, "
    "which 'forget' accepts.",
    {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact to remember, in one short sentence."},
            "scope": {"type": "string", "enum": ["user", "global"], "default": "user",
                      "description": "'user' for the current person, 'global' for everyone."},
        },
        "required": ["fact"],
    },
)
async def remember(ctx: ToolContext, fact: str, scope: str = "user") -> str:
    fact = " ".join(str(fact).split())
    if not fact:
        raise ToolError("the fact is empty")
    if scope not in ("user", "global"):
        raise ToolError("scope must be 'user' or 'global'")
    if scope == "global":
        if not ctx.is_admin:
            raise ToolError("only admins can save global notes; use scope 'user' instead")
        owner = GLOBAL_SCOPE
    else:
        owner = ctx.user.id
    memory = ctx.memory.add(owner, fact[:MAX_FACT_CHARS])
    log.info("memory %s saved by %s (%s)", memory.id, ctx.user.tag, scope)
    return f"Saved as memory #{memory.id} ({scope} scope)."


@tool(
    "forget",
    "Delete a saved memory by its id (the number shown with memories). You can forget your own "
    "memories; admins can forget any memory.",
    {
        "type": "object",
        "properties": {
            "memory_id": {"type": "integer", "description": "The id of the memory to delete."},
        },
        "required": ["memory_id"],
    },
)
async def forget(ctx: ToolContext, memory_id: int) -> str:
    try:
        mid = int(memory_id)
    except (TypeError, ValueError):
        raise ToolError("memory_id must be a number") from None
    if ctx.is_admin:
        deleted = ctx.memory.delete(mid)
    else:
        # The store only deletes when the memory belongs to this user, so a False
        # here means "not yours" or "no such memory".
        deleted = ctx.memory.delete(mid, user_id=ctx.user.id)
        if not deleted:
            raise ToolError(f"memory #{mid} is not one of your memories (or it does not exist)")
    if not deleted:
        raise ToolError(f"no memory #{mid}")
    log.info("memory %s forgotten by %s", mid, ctx.user.tag)
    return f"Forgot memory #{mid}."
