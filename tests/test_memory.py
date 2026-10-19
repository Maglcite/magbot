"""SQLite memory store: add/dedupe/cap, listing order, ownership, forgetting, persistence."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from magbot.memory import MAX_CHARS, Memory, MemoryStore

ALICE = "01AAAAAAAAAAAAAAAAAAAAAAA1"
BOB = "01BBBBBBBBBBBBBBBBBBBBBBB2"


@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


def texts(memories: list[Memory]) -> list[str]:
    return [m.text for m in memories]


def test_global_key():
    assert MemoryStore.GLOBAL == "*"


def test_add_returns_memory(store):
    before = time.time()
    m = store.add(ALICE, "likes tea")
    assert isinstance(m, Memory)
    assert m.id > 0
    assert m.user_id == ALICE
    assert m.text == "likes tea"
    assert before <= m.created_at <= time.time()


def test_add_strips_whitespace(store):
    assert store.add(ALICE, "  \n likes tea \t ").text == "likes tea"


def test_add_caps_length(store):
    m = store.add(ALICE, "x" * (MAX_CHARS + 100))
    assert MAX_CHARS == 500
    assert len(m.text) == 500
    assert store.for_user(ALICE)[0].text == "x" * 500


def test_add_cap_applies_after_strip(store):
    m = store.add(ALICE, "   " + "y" * 499 + "zz")
    assert m.text == "y" * 499 + "z"


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_add_rejects_empty(store, bad):
    with pytest.raises(ValueError):
        store.add(ALICE, bad)
    assert store.for_user(ALICE) == []


def test_exact_duplicate_same_user_returns_existing(store):
    first = store.add(ALICE, "likes tea")
    again = store.add(ALICE, "  likes tea  ")
    assert again.id == first.id
    assert again.created_at == first.created_at
    assert len(store.for_user(ALICE)) == 1


def test_same_text_different_users_are_separate(store):
    a = store.add(ALICE, "likes tea")
    b = store.add(BOB, "likes tea")
    assert a.id != b.id
    assert texts(store.for_user(ALICE)) == ["likes tea"]
    assert texts(store.for_user(BOB)) == ["likes tea"]


def test_near_duplicate_is_not_deduplicated(store):
    store.add(ALICE, "likes tea")
    store.add(ALICE, "Likes tea")
    store.add(ALICE, "likes tea.")
    assert len(store.for_user(ALICE)) == 3


def test_for_user_is_oldest_to_newest(store):
    for i in range(4):
        store.add(ALICE, f"fact {i}")
    assert texts(store.for_user(ALICE)) == ["fact 0", "fact 1", "fact 2", "fact 3"]


def test_for_user_limit_returns_newest_in_order(store):
    for i in range(5):
        store.add(ALICE, f"fact {i}")
    assert texts(store.for_user(ALICE, limit=3)) == ["fact 2", "fact 3", "fact 4"]


def test_for_user_default_limit_is_30(store):
    for i in range(35):
        store.add(ALICE, f"fact {i}")
    got = store.for_user(ALICE)
    assert len(got) == 30
    assert got[0].text == "fact 5"
    assert got[-1].text == "fact 34"


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_for_user_non_positive_limit_is_empty(store, limit):
    store.add(ALICE, "fact")
    assert store.for_user(ALICE, limit=limit) == []


def test_for_user_only_returns_own_rows(store):
    store.add(ALICE, "alice fact")
    store.add(BOB, "bob fact")
    store.add(MemoryStore.GLOBAL, "server fact")
    assert texts(store.for_user(ALICE)) == ["alice fact"]
    assert texts(store.for_user(MemoryStore.GLOBAL)) == ["server fact"]
    assert store.for_user("nobody") == []


def test_delete_owned_memory(store):
    m = store.add(ALICE, "fact")
    assert store.delete(m.id, ALICE) is True
    assert store.for_user(ALICE) == []
    assert store.delete(m.id, ALICE) is False


def test_delete_by_other_user_is_refused(store):
    m = store.add(ALICE, "fact")
    assert store.delete(m.id, BOB) is False
    assert texts(store.for_user(ALICE)) == ["fact"]


def test_delete_without_owner_removes_any(store):
    m = store.add(ALICE, "fact")
    assert store.delete(m.id) is True
    assert store.for_user(ALICE) == []


def test_global_note_cannot_be_deleted_by_a_user(store):
    g = store.add(MemoryStore.GLOBAL, "server rule")
    assert store.delete(g.id, ALICE) is False
    assert texts(store.for_user(MemoryStore.GLOBAL)) == ["server rule"]
    assert store.delete(g.id) is True  # admin path (no owner check)


def test_delete_unknown_id(store):
    assert store.delete(9999) is False
    assert store.delete(9999, ALICE) is False


def test_ids_are_not_reused_after_delete(store):
    first = store.add(ALICE, "one")
    store.delete(first.id)
    second = store.add(ALICE, "two")
    assert second.id > first.id


def test_forget_user_removes_only_that_user(store):
    store.add(ALICE, "a1")
    store.add(ALICE, "a2")
    store.add(BOB, "b1")
    store.add(MemoryStore.GLOBAL, "server")
    assert store.forget_user(ALICE) == 2
    assert store.for_user(ALICE) == []
    assert texts(store.for_user(BOB)) == ["b1"]
    assert texts(store.for_user(MemoryStore.GLOBAL)) == ["server"]


def test_forget_user_unknown_returns_zero(store):
    assert store.forget_user(ALICE) == 0


def test_forget_then_add_again(store):
    store.add(ALICE, "old")
    store.forget_user(ALICE)
    store.add(ALICE, "old")  # no stale duplicate left behind
    assert texts(store.for_user(ALICE)) == ["old"]


def test_memory_db_persists_and_reopens(tmp_path):
    path = tmp_path / "nested" / "memory.db"  # parent directory is created
    s = MemoryStore(path)
    kept = s.add(ALICE, "persisted")
    s.close()

    reopened = MemoryStore(path)  # schema creation is idempotent
    try:
        got = reopened.for_user(ALICE)
        assert [(m.id, m.text) for m in got] == [(kept.id, "persisted")]
        assert reopened.add(ALICE, "persisted").id == kept.id  # dedupe survives restart
    finally:
        reopened.close()


def test_str_and_path_both_accepted(tmp_path):
    s1 = MemoryStore(str(tmp_path / "a.db"))
    s2 = MemoryStore(tmp_path / "b.db")
    try:
        s1.add(ALICE, "x")
        s2.add(ALICE, "y")
        assert texts(s1.for_user(ALICE)) == ["x"]
        assert texts(s2.for_user(ALICE)) == ["y"]
    finally:
        s1.close()
        s2.close()


def test_memory_store_is_usable_from_threads(store):
    def work(i: int) -> None:
        store.add(ALICE, f"fact {i}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(40)))
    assert len(store.for_user(ALICE, limit=100)) == 40


def test_close_is_safe_to_call_twice(tmp_path):
    s = MemoryStore(tmp_path / "m.db")
    s.close()
    s.close()
