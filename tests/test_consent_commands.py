"""Consent command parsing: the legacy strict-form CASES table, ported.

Every case from the old bot's tests/test_commands.py is kept, run against
the default bot names from config.
"""
from __future__ import annotations

import pytest

from magbot.config import BotConfig, Config
from magbot.consent import PRESET_OPTIN_MESSAGE, optin_message, parse_command

NAMES = BotConfig().names  # default: magbot, mag bot, mag-bot, fembot

CASES = [
    # (text, pinged, is_dm, expect_optin, expect_optout, note)
    # ── the two accepted forms ───────────────────────────────────────────────
    ("magbot opt in", False, False, True, False, "exact accepted form"),
    ("magbot opt out", False, False, False, True, "exact accepted form"),
    ("Magbot, opt me out please", False, False, False, True, "name + phrase, polite"),
    ("mag bot opt out", False, False, False, True, "spaced name variant"),
    ("mag-bot opt-in", False, False, True, False, "hyphenated name + phrase"),
    ("fembot opt out", False, False, False, True, "server display-name alias"),
    ("hey fembot, can you opt me in?", False, False, True, False, "alias + polite phrase"),
    ("ok magbot pls opt out", False, False, False, True, "filler before name"),
    ("opt in", True, False, True, False, "@-ping + bare phrase"),
    ("opt out", True, False, False, True, "@-ping + bare phrase"),
    ("opt out", False, True, False, True, "DM with the bot + bare phrase"),
    ("opt in", False, True, True, False, "DM with the bot + bare phrase"),
    # ── rejected: no name / not addressed ───────────────────────────────────
    ("opt out", False, False, False, False, "bare phrase in a channel"),
    ("optout", False, False, False, False, "bare phrase, no space"),
    ("opt me out", False, False, False, False, "bare loose phrase"),
    ("!optout", False, False, False, False, "bang command no longer accepted"),
    ("!optin", False, False, False, False, "bang command no longer accepted"),
    ("ok, opt in", False, False, False, False, "filler form no longer accepted"),
    ("i consent", False, False, False, False, "inferred statement no longer accepted"),
    ("you can use my messages", False, False, False, False, "inferred statement no longer accepted"),
    ("i don't want my messages used", False, False, False, False, "inferred statement, unaddressed"),
    ("remove me from your memory", False, False, False, False, "inferred statement, unaddressed"),
    # ── rejected: prose ─────────────────────────────────────────────────────
    ("we should opt out of that newsletter lol", False, False, False, False, "prose, no name"),
    ("can everyone opt out of the ping role", False, False, False, False, "prose about others"),
    ("i might opt in later idk", False, False, False, False, "prose"),
    ("random chatter about nothing", False, False, False, False, "no trigger"),
    # ── rejected: name present but phrase too far / wrong shape ─────────────
    ("magbot is the best bot in the world, anyway i opt out of that newsletter",
     False, False, False, False, "phrase >40 chars after the name"),
    ("magbot my man, whats up", False, False, False, False, "name, no phrase"),
    # ── guards ──────────────────────────────────────────────────────────────
    ("magbot, should we opt out of that?", False, False, False, False, "advice-question"),
    ("magbot, why would anyone opt in to that?", False, False, False, False, "advice-question"),
    ("magbot i don't want to opt out", False, False, False, False, "negated"),
    ("magbot no need to opt me out", False, False, False, False, "negated"),
    ("magbot i won't opt out", False, False, False, False, "negated"),
    ("magbot i don't want to opt in", False, False, False, False, "negated"),
    ("magbot did NOT say opt me in", False, False, False, False, "negated"),
    ("magbot i am opting out right now", False, False, False, True, "present-tense intent"),
    ("magbot i'd like to opt out", False, False, False, True, "polite intent"),
]


@pytest.mark.parametrize("text,pinged,is_dm,expect_optin,expect_optout,note", CASES)
def test_legacy_case(text, pinged, is_dm, expect_optin, expect_optout, note):
    expected = "in" if expect_optin else "out" if expect_optout else None
    assert parse_command(text, NAMES, pinged=pinged, is_dm=is_dm) == expected, note


# ── beyond the legacy table: names come from config ──────────────────────────

def test_configured_names_replace_defaults():
    assert parse_command("robot opt in", ["robot"], pinged=False, is_dm=False) == "in"
    assert parse_command("magbot opt in", ["robot"], pinged=False, is_dm=False) is None
    assert parse_command("mag bot opt out", ["robot"], pinged=False, is_dm=False) is None


def test_name_is_a_whole_word():
    assert parse_command("robot opt in", ["bot"], pinged=False, is_dm=False) is None
    assert parse_command("bot opt in", ["bot"], pinged=False, is_dm=False) == "in"


def test_name_punctuation_is_literal():
    assert parse_command("r.2 opt in", ["r.2"], pinged=False, is_dm=False) == "in"
    assert parse_command("rX2 opt in", ["r.2"], pinged=False, is_dm=False) is None


def test_multiword_name_allows_flexible_whitespace():
    assert parse_command("skybot opt in", ["sky bot"], pinged=False, is_dm=False) == "in"
    assert parse_command("sky   bot opt out", ["sky bot"], pinged=False, is_dm=False) == "out"


def test_no_names_only_pings_and_dms_count():
    assert parse_command("magbot opt in", [], pinged=False, is_dm=False) is None
    assert parse_command("opt in", [], pinged=True, is_dm=False) == "in"
    assert parse_command("opt out", [], pinged=False, is_dm=True) == "out"


def test_opt_in_checked_before_opt_out():
    assert parse_command("magbot opt in, not opt out", NAMES, pinged=False, is_dm=False) == "in"


def test_empty_text():
    assert parse_command("", NAMES, pinged=True, is_dm=True) is None


def test_optin_message_uses_first_configured_name():
    default = Config()
    assert optin_message(default) == PRESET_OPTIN_MESSAGE.format(name="magbot")
    assert "`magbot opt in`" in optin_message(default)

    custom = Config(bot=BotConfig(names=["robot", "magbot"]))
    msg = optin_message(custom)
    assert "`robot opt in`" in msg and "`robot opt out`" in msg
    assert "magbot" not in msg
