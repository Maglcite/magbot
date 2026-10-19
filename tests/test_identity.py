from types import SimpleNamespace

from magbot.config import Config
from magbot.identity import User, can_shell, from_message, is_admin


def test_admin_and_shell_are_separate_and_exact():
    cfg = Config()
    cfg.bot.admins = ["alice#1001", "01CCCCCCCCCCCCCCCCCCCCCCC3"]
    cfg.bot.shell_users = ["alice#1001"]
    alice = User("01AAAAAAAAAAAAAAAAAAAAAAA1", "alice#1001", "Alice")
    carol = User("01CCCCCCCCCCCCCCCCCCCCCCC3", "carol#1003", "Carol")
    impostor = User("01XXXXXXXXXXXXXXXXXXXXXXX9", "alice#9999", "Alice")
    assert is_admin(alice, cfg) and can_shell(alice, cfg)
    assert is_admin(carol, cfg) and not can_shell(carol, cfg)   # admins don't get the shell
    assert not is_admin(impostor, cfg) and not can_shell(impostor, cfg)


def test_from_message_handles_missing_author():
    class Msg:
        author_id = "01AAAAAAAAAAAAAAAAAAAAAAA1"

        @property
        def author(self):
            raise LookupError("not cached")

    u = from_message(Msg())
    assert u.id == u.tag == "01AAAAAAAAAAAAAAAAAAAAAAA1"
    named = from_message(SimpleNamespace(author_id="1", author=SimpleNamespace(
        name="bob", discriminator="2002", display_name="Bobby")))
    assert (named.tag, named.display_name) == ("bob#2002", "Bobby")
