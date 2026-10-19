"""Consent store: policy, list matching, toggles, file formats and the prompt cooldown.

All files are written under tmp_path. The lists start from tests/fixtures/optin.list.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from magbot import consent as consent_mod
from magbot.config import BotConfig, Config, ConsentConfig
from magbot.consent import Consent
from magbot.identity import User

FIXTURE = Path(__file__).parent / "fixtures" / "optin.list"

ALICE = "01AAAAAAAAAAAAAAAAAAAAAAA1"
BOB = "01BBBBBBBBBBBBBBBBBBBBBBB2"
CAROL = "01CCCCCCCCCCCCCCCCCCCCCCC3"
BOT = "01DDDDDDDDDDDDDDDDDDDDDDD4"
STRANGER = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
OTHER = "01YYYYYYYYYYYYYYYYYYYYYYYYYY"


def make_config(data_dir: Path, *, always_allow=(), always_exclude=(), cooldown=43200) -> Config:
    return Config(
        bot=BotConfig(data_dir=str(data_dir)),
        consent=ConsentConfig(prompt_cooldown=cooldown,
                              always_allow=list(always_allow),
                              always_exclude=list(always_exclude)),
        self_id=BOT,
    )


def user(uid: str, tag: str | None = None) -> User:
    tag = tag or uid
    return User(uid, tag, tag.split("#", 1)[0])


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    shutil.copyfile(FIXTURE, d / "optin.list")
    return d


@pytest.fixture
def consent(data_dir: Path) -> Consent:
    return Consent(make_config(data_dir))


# ── allow / deny policy ─────────────────────────────────────────────────────

def test_grandfathered_users_allowed(consent):
    assert consent.is_allowed(user(ALICE, "alice#1001"))  # id + tag
    assert consent.is_allowed(user(BOB))                  # bare id entry
    assert consent.is_allowed(user(CAROL, "carol#1003"))


def test_rename_proof_tag_matching(consent):
    # Same tag, no id: the entry still matches (rename-proof).
    assert consent.is_allowed(user("", "dana_old#1004"))
    # Same id under a new name: the id entry still matches.
    assert consent.is_allowed(user(ALICE, "alice_renamed#2002"))


def test_tag_match_is_case_insensitive(consent):
    assert consent.is_allowed(user("", "ALICE#1001"))


def test_exact_tag_or_bare_name_only(consent):
    # "alice#1001" is on the list; a different discriminator is a different account.
    assert not consent.is_allowed(user("", "alice#9999"))
    assert not consent.is_allowed(user(STRANGER, "stranger#0001"))


def test_bare_name_entry_matches_any_discriminator(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "optin.list").write_text("TAG erin\n")
    c = Consent(make_config(d))
    assert c.is_allowed(user("", "erin#4444"))
    assert not c.is_allowed(user("", "erinx#4444"))


def test_bare_entries_by_length(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    long_id = "01EEEEEEEEEEEEEEEEEEEEEEE5"
    (d / "optin.list").write_text(f"{long_id}\nfrank\n")  # >12 chars -> id, else tag
    c = Consent(make_config(d))
    assert c.is_allowed(user(long_id))
    assert c.is_allowed(user("", "frank#0007"))
    assert not c.is_allowed(user("", long_id + "x#1"))


def test_unknown_user_blocked(consent):
    assert not consent.is_allowed(user(STRANGER, "stranger2#0001"))
    assert not consent.is_allowed(user("", "stranger2#0001"))


def test_self_id_always_allowed(data_dir):
    c = Consent(make_config(data_dir))
    assert c.is_allowed(user(BOT, "magbot#0000"))
    # Without a configured self id, the bot's id gets no free pass.
    cfg = make_config(data_dir)
    cfg.self_id = ""
    assert not Consent(cfg).is_allowed(user(BOT, "magbot#0000"))


def test_allow_then_deny_then_reallow(consent, data_dir):
    assert consent.allow(user(STRANGER, "stranger#0001")) is True
    assert consent.is_allowed(user(STRANGER, "stranger#0001"))
    assert consent.allow(user(STRANGER, "stranger#0001")) is False  # idempotent

    assert consent.deny(user(STRANGER, "stranger#0001")) is True
    assert not consent.is_allowed(user(STRANGER, "stranger#0001"))
    assert consent.is_excluded(user(STRANGER, "stranger#0001"))
    assert consent.deny(user(STRANGER, "stranger#0001")) is False  # already excluded

    # Opting back in lifts the block.
    assert consent.allow(user(STRANGER, "stranger#0001")) is True
    assert consent.is_allowed(user(STRANGER, "stranger#0001"))
    assert not consent.is_excluded(user(STRANGER, "stranger#0001"))


def test_allow_writes_id_and_tag_lines(consent, data_dir):
    consent.allow(user(STRANGER, "stranger#0001"))
    text = (data_dir / "optin.list").read_text()
    assert text.endswith(f"ID {STRANGER}\nTAG stranger#0001\n")


def test_unknown_author_writes_only_id(consent, data_dir):
    # identity.from_message uses the id as the tag when the author is not cached.
    consent.allow(user(STRANGER, STRANGER))
    assert (data_dir / "optin.list").read_text().endswith(f"ID {STRANGER}\n")


def test_allow_clears_opt_out(consent, data_dir):
    consent.deny(user(STRANGER, "stranger#0001"))
    consent.allow(user(STRANGER, "stranger#0001"))
    assert (data_dir / "optout.list").read_text().count(STRANGER) == 0


def test_deny_removes_from_optin(consent, data_dir):
    consent.deny(user(ALICE, "alice#1001"))
    assert not consent.is_allowed(user(ALICE, "alice#1001"))
    assert ALICE not in (data_dir / "optin.list").read_text()


def test_toggle_is_identity_precise(consent, data_dir):
    (data_dir / "optin.list").write_text("TAG bob#1\nTAG bob#2\n")
    consent.deny(user("", "bob#1"))
    assert (data_dir / "optin.list").read_text() == "TAG bob#2\n"


def test_tag_without_discriminator_drops_stale_variants(consent, data_dir):
    (data_dir / "optin.list").write_text("TAG dana#1\nTAG dana#2\nTAG other#3\n")
    consent.deny(user("", "dana"))
    assert (data_dir / "optin.list").read_text() == "TAG other#3\n"


def test_legacy_long_id_written_as_tag_still_grants(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "optin.list").write_text(f"TAG {BOB}\n")
    # The user's tag is not the id here, so only the legacy long-id clause can match.
    assert Consent(make_config(d)).is_allowed(user(BOB, "bob#2222"))


# ── exclusions and config ───────────────────────────────────────────────────

def test_excluded_wins_over_optin(consent, data_dir):
    (data_dir / "optout.list").write_text(f"ID {ALICE}\n")
    assert consent.is_excluded(user(ALICE, "alice#1001"))
    assert not consent.is_allowed(user(ALICE, "alice#1001"))


def test_excluded_wins_over_always_allow(data_dir):
    c = Consent(make_config(data_dir, always_allow=[ALICE], always_exclude=[ALICE]))
    assert not c.is_allowed(user(ALICE))
    assert c.is_excluded(user(ALICE))


def test_always_allow_ids_and_tags(data_dir):
    c = Consent(make_config(data_dir, always_allow=[OTHER, "ivy#7777"]))
    assert c.is_allowed(user(OTHER))
    assert c.is_allowed(user("", "ivy#7777"))
    assert not c.is_allowed(user("", "ivy#8888"))
    # Config entries are not written to the file.
    assert "ivy" not in (data_dir / "optin.list").read_text()


def test_always_exclude_ids_and_tags(data_dir):
    c = Consent(make_config(data_dir, always_exclude=[CAROL, "ivy#7777"]))
    assert c.is_excluded(user(CAROL, "carol#1003"))
    assert c.is_excluded(user("", "ivy#7777"))
    assert not c.is_excluded(user(BOB))
    assert not c.is_allowed(user(CAROL, "carol#1003"))  # excluded beats the list


def test_always_exclude_tag_form_with_prefix(data_dir):
    c = Consent(make_config(data_dir, always_exclude=["TAG ivy#7777", f"ID {OTHER}"]))
    assert c.is_excluded(user("", "ivy#7777"))
    assert c.is_excluded(user(OTHER))


def test_deny_already_excluded_by_config_returns_false(data_dir):
    c = Consent(make_config(data_dir, always_exclude=[STRANGER]))
    assert c.deny(user(STRANGER, "stranger#0001")) is False


def test_counts(data_dir):
    c = Consent(make_config(data_dir))
    # fixture: ids 01AAA, 01BBB, 01CCC; tags alice#1001, carol#1003, dana_old#1004
    assert c.counts() == (3, 3, 0)

    (data_dir / "optout.list").write_text(f"ID {STRANGER}\n")
    c2 = Consent(make_config(data_dir, always_allow=["ivy#7777"], always_exclude=["x#1"]))
    assert c2.counts() == (3, 4, 2)


# ── files: formats, comments, round trip, atomicity ─────────────────────────

def test_comments_and_blank_lines_survive_toggles(consent, data_dir):
    (data_dir / "optin.list").write_text(
        "# header comment\n\nID " + ALICE + "\n# keep me\nTAG carol#1003\n"
    )
    consent.deny(user(ALICE, "alice#1001"))
    assert (data_dir / "optin.list").read_text() == "# header comment\n\n# keep me\nTAG carol#1003\n"


def test_comments_are_not_identities(data_dir):
    (data_dir / "optin.list").write_text("# ID " + STRANGER + "\n")
    assert not Consent(make_config(data_dir)).is_allowed(user(STRANGER))


def test_round_trip_through_a_fresh_instance(data_dir):
    cfg = make_config(data_dir)
    Consent(cfg).allow(user(STRANGER, "stranger#0001"))
    Consent(cfg).deny(user(OTHER, "other#0002"))
    fresh = Consent(cfg)
    assert fresh.is_allowed(user(STRANGER, "stranger#0001"))
    assert fresh.is_excluded(user(OTHER, "other#0002"))
    assert not fresh.is_allowed(user(OTHER, "other#0002"))


def test_lists_are_read_live(consent, data_dir):
    assert not consent.is_allowed(user(STRANGER, "late#0003"))
    with open(data_dir / "optin.list", "a", encoding="utf-8") as f:
        f.write("TAG late#0003\n")
    assert consent.is_allowed(user(STRANGER, "late#0003"))


def test_missing_data_dir_is_created_on_write(tmp_path):
    d = tmp_path / "nested" / "data"
    c = Consent(make_config(d))
    assert not c.is_allowed(user(STRANGER))  # reads never create anything
    assert not d.exists()
    c.allow(user(STRANGER, "stranger#0001"))
    assert (d / "optin.list").exists()


def test_no_temp_files_left_behind(consent, data_dir):
    consent.allow(user(STRANGER, "stranger#0001"))
    consent.deny(user(STRANGER, "stranger#0001"))
    consent.mark_prompted(user(OTHER))
    assert sorted(p.name for p in data_dir.iterdir()) == [
        "optin-prompted.json", "optin.list", "optout.list",
    ]


def test_failed_write_keeps_original_file(consent, data_dir, monkeypatch):
    before = (data_dir / "optin.list").read_text()

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(consent_mod.os, "replace", boom)
    with pytest.raises(OSError):
        consent.allow(user(STRANGER, "stranger#0001"))
    assert (data_dir / "optin.list").read_text() == before
    assert not (data_dir / "optin.list.tmp").exists()


def test_bad_optin_file_is_not_clobbered(data_dir, monkeypatch):
    # A list we cannot read must not be rewritten as if it were empty.
    c = Consent(make_config(data_dir))
    original_read = Path.read_text

    def read(self, *a, **kw):
        if self.name == "optin.list":
            raise PermissionError("nope")
        return original_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError):
        c.allow(user(STRANGER, "stranger#0001"))


# ── opt-in prompt cooldown ──────────────────────────────────────────────────

def test_prompt_then_cooldown(consent):
    stranger = user(STRANGER, "stranger#0001")
    assert consent.should_prompt(stranger)
    consent.mark_prompted(stranger)
    assert not consent.should_prompt(stranger)


def test_prompt_cooldown_expires(data_dir):
    c = Consent(make_config(data_dir, cooldown=43200))
    now = time.time()
    (data_dir / "optin-prompted.json").write_text(
        json.dumps({STRANGER: now - 43201, OTHER: now - 100})
    )
    assert c.should_prompt(user(STRANGER))
    assert not c.should_prompt(user(OTHER))


def test_prompt_state_file_format(consent, data_dir):
    consent.mark_prompted(user(STRANGER, "stranger#0001"))
    consent.mark_prompted(user(OTHER))
    state = json.loads((data_dir / "optin-prompted.json").read_text())
    assert set(state) == {STRANGER, OTHER}
    assert all(isinstance(v, int) for v in state.values())
    assert abs(state[STRANGER] - time.time()) < 60


def test_prompt_state_corrupt_or_not_a_dict(data_dir):
    (data_dir / "optin-prompted.json").write_text("[1, 2, 3]")
    c = Consent(make_config(data_dir))
    assert c.should_prompt(user(STRANGER))
    c.mark_prompted(user(STRANGER))  # overwrites the bad state with a dict
    assert json.loads((data_dir / "optin-prompted.json").read_text()) == {
        STRANGER: pytest.approx(time.time(), abs=60)
    }


def test_no_id_never_prompted_or_recorded(consent, data_dir):
    nobody = User("", "ghost#0000", "ghost")
    assert not consent.should_prompt(nobody)
    consent.mark_prompted(nobody)
    assert not (data_dir / "optin-prompted.json").exists()
