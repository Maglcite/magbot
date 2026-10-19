#!/usr/bin/env python3
"""Import a Hermes Agent profile's memories (MEMORY.md / USER.md) into magbot.

Hermes keeps memories as entries separated by lines containing only "§".
An entry that mentions a "name#1234" tag becomes a memory of that user;
anything else becomes a global note. Consent is respected: entries about
users who are not opted in (or who opted out) are skipped, never imported.

Tags are matched to Stoat user ids through the `ID` / `TAG` line pairs in
optin.list, plus an optional --map file of "<user id> <name#1234>" lines.

    python scripts/import_hermes_memory.py --config /opt/magbot/config.toml \\
        ~/.hermes/profiles/fembot/memories/MEMORY.md \\
        ~/.hermes/profiles/fembot/memories/USER.md --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from magbot import config as config_mod  # noqa: E402
from magbot.consent import OPTIN_FILE, Consent  # noqa: E402
from magbot.identity import User  # noqa: E402
from magbot.memory import MemoryStore  # noqa: E402

TAG_RE = re.compile(r"(?<![\w#])([A-Za-z0-9_.\-]{2,32}#\d{4})(?!\d)")


def read_entries(path: Path) -> list[str]:
    """Entries of a Hermes memory file, whitespace-normalised."""
    chunks = re.split(r"^\s*§\s*$", path.read_text(encoding="utf-8"), flags=re.M)
    return [" ".join(c.split()) for c in chunks if c.strip()]


def tag_to_id(optin_list: Path, extra: Path | None) -> dict[str, str]:
    """Map lower-cased tags to user ids from optin.list ID/TAG pairs and --map."""
    mapping: dict[str, str] = {}
    if optin_list.exists():
        # A TAG belongs to the ID line directly above it, unless that ID is part
        # of a run of several ID lines (then the pairing is ambiguous). A TAG
        # without its own ID line stays unmapped.
        prev_kind = prev_value = None
        lone_id = False
        for raw in optin_list.read_text(encoding="utf-8").splitlines():
            parts = raw.strip().split(None, 1)
            kind, value = (parts[0].upper(), parts[1].strip()) if len(parts) == 2 else (None, None)
            if kind == "TAG" and prev_kind == "ID" and lone_id:
                mapping.setdefault(value.lower(), prev_value)
            if kind == "ID":
                lone_id = prev_kind != "ID"
            prev_kind, prev_value = kind, value
    if extra:
        for raw in extra.read_text(encoding="utf-8").splitlines():
            parts = raw.strip().split(None, 1)
            if len(parts) == 2 and not raw.startswith("#"):
                mapping[parts[1].strip().lower()] = parts[0]
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path, help="Hermes MEMORY.md / USER.md files")
    ap.add_argument("--config", help="magbot config.toml (default: $MAGBOT_CONFIG)")
    ap.add_argument("--map", type=Path, help='extra "<user id> <name#1234>" lines')
    ap.add_argument("--skip", action="append", default=[],
                    help="regex; entries matching it are not imported (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be imported")
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    consent = Consent(cfg)
    ids = tag_to_id(cfg.data_path / OPTIN_FILE, args.map)
    skips = [re.compile(s, re.I) for s in args.skip]
    store = None if args.dry_run else MemoryStore(cfg.data_path / "memory.db")

    counts = {"user": 0, "global": 0, "no consent": 0, "unknown id": 0, "skipped": 0}
    for path in args.files:
        for entry in read_entries(path):
            if any(s.search(entry) for s in skips):
                counts["skipped"] += 1
                continue
            tags = TAG_RE.findall(entry)
            if not tags:
                owner, label = MemoryStore.GLOBAL, "global"
            else:
                tag = tags[0]
                uid = ids.get(tag.lower())
                if uid is None:
                    counts["unknown id"] += 1
                    print(f"  no id for {tag}: not imported")
                    continue
                if not consent.is_allowed(User(uid, tag, tag)):
                    counts["no consent"] += 1
                    print(f"  {tag} is not opted in: not imported")
                    continue
                owner, label = uid, "user"
            counts[label] += 1
            if store is not None:
                store.add(owner, entry)
            if args.dry_run:
                print(f"  [{label}] {entry[:100]}")
    if store is not None:
        store.close()
    print(", ".join(f"{k}: {v}" for k, v in counts.items())
          + (" (dry run, nothing written)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
