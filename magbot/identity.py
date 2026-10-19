"""Who sent a message, and what they are allowed to do."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class User:
    id: str
    tag: str            # "name#1234", or just the name / id when unknown
    display_name: str

    @property
    def bare(self) -> str:
        """The tag without its discriminator."""
        return self.tag.split("#", 1)[0]

    def matches(self, entries) -> bool:
        """True if any entry is exactly this user's id or full tag.

        Deliberately strict (no bare-name match): this gates admin powers, and
        anyone can register "alice#9999" next to the real "alice#1001".
        """
        return any(e and (e == self.id or e == self.tag) for e in entries)


def from_message(message) -> User:
    """Build a User from a stoat Message.

    History messages may not have their author cached, in which case the
    `author` property raises; fall back to the always-present author_id.
    """
    uid = str(getattr(message, "author_id", "") or "")
    try:
        author = message.author
    except Exception:
        author = None
    if author is None:
        return User(uid, uid, uid)
    name = getattr(author, "name", None) or uid
    disc = getattr(author, "discriminator", None)
    display = getattr(author, "display_name", None) or name
    return User(uid, f"{name}#{disc}" if disc else name, display)


def is_admin(user: User, config: Config) -> bool:
    return user.matches(config.bot.admins)


def can_shell(user: User, config: Config) -> bool:
    """Shell access is granted explicitly; being an admin is not enough.

    The tool runs as the bot's own OS user, so keep this list short.
    """
    return user.matches(config.bot.shell_users)
