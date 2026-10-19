"""Long-term memory: short facts the bot keeps about users, in SQLite.

Each row is one fact with the user it is about. Use MemoryStore.GLOBAL as the
user_id for notes about everyone or the server. ":memory:" works for tests.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_CHARS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_by_user ON memories (user_id, id);
"""

_COLUMNS = "id, user_id, text, created_at"


@dataclass
class Memory:
    id: int
    user_id: str
    text: str
    created_at: float


class MemoryStore:
    GLOBAL = "*"  # user_id for notes about everyone / the server

    def __init__(self, path: str | Path):
        self._lock = threading.Lock()
        target = str(path)
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(target, check_same_thread=False)
        with self._lock:
            self._db.executescript(_SCHEMA)

    def add(self, user_id: str, text: str) -> Memory:
        """Save a fact. Strips whitespace and caps the length. An exact duplicate
        for the same user returns the existing entry instead of a new one."""
        clean = text.strip()[:MAX_CHARS]
        if not clean:
            raise ValueError("memory text is empty")
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = ? AND text = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, clean),
            ).fetchone()
            if row:
                return Memory(*row)
            now = time.time()
            cur = self._db.execute(
                "INSERT INTO memories (user_id, text, created_at) VALUES (?, ?, ?)",
                (user_id, clean, now),
            )
            self._db.commit()
            return Memory(cur.lastrowid, user_id, clean, now)

    def for_user(self, user_id: str, limit: int = 30) -> list[Memory]:
        """The newest `limit` memories for `user_id`, oldest first."""
        if limit <= 0:
            return []
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM memories WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [Memory(*row) for row in reversed(rows)]

    def delete(self, memory_id: int, user_id: str | None = None) -> bool:
        """Delete one memory. With `user_id`, only if that user owns it. True if deleted."""
        sql = "DELETE FROM memories WHERE id = ?"
        args: list = [memory_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount > 0

    def forget_user(self, user_id: str, tag: str = "") -> int:
        """Delete every memory about `user_id` (used on opt-out). With `tag`, also the
        memories still pending under that tag. Returns the count."""
        keys = [user_id] + ([self.tag_key(tag)] if tag.strip() else [])
        with self._lock:
            cur = self._db.execute(
                f"DELETE FROM memories WHERE user_id IN ({','.join('?' * len(keys))})", keys)
            self._db.commit()
            count = cur.rowcount
        if count:
            log.info("memory: forgot %d entries for %s", count, user_id)
        return count

    @staticmethod
    def tag_key(tag: str) -> str:
        """The user_id under which memories for a not-yet-known user are kept, by tag."""
        return "tag:" + tag.strip().lower()

    def claim(self, tag: str, user_id: str) -> int:
        """Move the memories kept under `tag_key(tag)` to `user_id`. Returns the count moved.

        A pending memory whose exact text `user_id` already has is deleted instead.
        Called on every message, so with nothing pending it is a single indexed lookup.
        """
        key = self.tag_key(tag)
        if key == "tag:" or not user_id:
            return 0
        with self._lock:
            pending = self._db.execute(
                "SELECT id, text FROM memories WHERE user_id = ? ORDER BY id", (key,)
            ).fetchall()
            if not pending:
                return 0
            have = {text for (text,) in self._db.execute(
                "SELECT text FROM memories WHERE user_id = ?", (user_id,))}
            moved = 0
            for row_id, text in pending:
                if text in have:
                    self._db.execute("DELETE FROM memories WHERE id = ?", (row_id,))
                else:
                    self._db.execute("UPDATE memories SET user_id = ? WHERE id = ?",
                                     (user_id, row_id))
                    have.add(text)
                    moved += 1
            self._db.commit()
        if moved:
            log.info("memory: claimed %d pending entries for %s", moved, user_id)
        return moved

    def pending_count(self) -> int:
        """How many memories are still kept under a tag key (users not yet claimed)."""
        with self._lock:
            (count,) = self._db.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id LIKE 'tag:%'").fetchone()
        return count

    def close(self) -> None:
        with self._lock:
            self._db.close()
