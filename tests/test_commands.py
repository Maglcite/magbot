#!/usr/bin/env python3
"""Strict command form: only "magbot opt in" / "magbot opt out" (or @-ping / DM + phrase)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import consent

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

fails = 0
for text, pinged, is_dm, ein, eout, note in CASES:
    gin = consent.optin_requested(text, pinged=pinged, is_dm=is_dm)
    gout = consent.optout_requested(text, pinged=pinged, is_dm=is_dm)
    ok = (gin == ein) and (gout == eout)
    if not ok:
        fails += 1
    print(f"{'ok ' if ok else 'FAIL'} in={gin:<5} out={gout:<5} ping={str(pinged):<5} dm={str(is_dm):<5} | {text!r}  ({note})")
print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
