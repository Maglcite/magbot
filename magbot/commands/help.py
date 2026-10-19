"""/help: list the commands, admin-only ones for admins only."""
from __future__ import annotations

from . import REGISTRY, CommandContext, command


def help_text(is_admin: bool) -> str:
    """The /help reply: one line per command, with aliases and help text."""
    seen: set[int] = set()
    lines = ["**Commands**"]
    for cmd in REGISTRY.values():  # several spellings map to one Command
        if id(cmd) in seen or (cmd.admin and not is_admin):
            continue
        seen.add(id(cmd))
        head = f"• `/{cmd.name}`"
        if cmd.aliases:
            head += " (also " + ", ".join(f"`{a}`" for a in cmd.aliases) + ")"
        if cmd.admin:
            head += " (admins only)"
        lines.append(f"{head}: {cmd.help}" if cmd.help else head)
    return "\n".join(lines)


@command("help", help="List the commands you can use.")
async def help_cmd(ctx: CommandContext) -> None:
    await ctx.reply(help_text(ctx.is_admin))
