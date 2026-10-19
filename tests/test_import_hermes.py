import subprocess
import sys
from pathlib import Path

from magbot.memory import MemoryStore

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_hermes_memory.py"


def test_import_respects_consent_and_maps_ids(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "optin.list").write_text(
        "# comment\nID 01AAAAAAAAAAAAAAAAAAAAAAA1\nTAG alice#1001\n"
        "ID 01CCCCCCCCCCCCCCCCCCCCCCC3\nTAG carol#1003\n")
    (data / "optout.list").write_text("ID 01CCCCCCCCCCCCCCCCCCCCCCC3\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[bot]\ndata_dir = "{data}"\nwww_dir = "{tmp_path / "www"}"\n')
    mem = tmp_path / "MEMORY.md"
    mem.write_text("alice#1001: likes birds.\n§\nBe kind to everyone.\n§\n"
                   "carol#1003: opted out, must not import.\n§\nbob#2002: no id known.\n§\n"
                   "Heartbeat: reply ok.\n§\n")

    out = subprocess.run([sys.executable, str(SCRIPT), "--config", str(cfg), str(mem),
                          "--skip", "^Heartbeat:"],
                         capture_output=True, text=True, check=True).stdout
    assert "user: 1, global: 1, pending: 0, no consent: 2, skipped: 1" in out

    store = MemoryStore(data / "memory.db")
    assert [m.text for m in store.for_user("01AAAAAAAAAAAAAAAAAAAAAAA1")] == ["alice#1001: likes birds."]
    assert [m.text for m in store.for_user(MemoryStore.GLOBAL)] == ["Be kind to everyone."]
    assert store.for_user("01CCCCCCCCCCCCCCCCCCCCCCC3") == []
    assert store.pending_count() == 0
    store.close()


def test_opted_in_tag_without_id_is_kept_pending_and_claimed_later(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # dave is opted in by tag only; erin is opted out; frank is not on any list.
    (data / "optin.list").write_text("TAG dave#4004\nTAG erin#5005\n")
    (data / "optout.list").write_text("TAG erin#5005\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[bot]\ndata_dir = "{data}"\nwww_dir = "{tmp_path / "www"}"\n')
    mem = tmp_path / "MEMORY.md"
    mem.write_text("dave#4004 likes chess.\n§\nerin#5005 has a secret.\n§\n"
                   "frank#6006 is unknown.\n§\n")

    out = subprocess.run([sys.executable, str(SCRIPT), "--config", str(cfg), str(mem)],
                         capture_output=True, text=True, check=True).stdout
    assert "user: 0, global: 0, pending: 1, no consent: 2, skipped: 0" in out
    assert "dave#4004" in out and "kept pending" in out

    store = MemoryStore(data / "memory.db")
    assert store.pending_count() == 1
    assert [m.text for m in store.for_user(MemoryStore.tag_key("dave#4004"))] == [
        "dave#4004 likes chess."]
    assert store.for_user(MemoryStore.tag_key("erin#5005")) == []
    assert store.for_user(MemoryStore.tag_key("frank#6006")) == []

    # When dave next talks, the bot claims by tag and the entry attaches to his id.
    assert store.claim("dave#4004", "01DDDDDDDDDDDDDDDDDDDDDDD4") == 1
    assert [m.text for m in store.for_user("01DDDDDDDDDDDDDDDDDDDDDDD4")] == [
        "dave#4004 likes chess."]
    assert store.pending_count() == 0
    store.close()


def test_dry_run_writes_nothing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[bot]\ndata_dir = "{data}"\n')
    mem = tmp_path / "USER.md"
    mem.write_text("A global note.\n§\n")
    subprocess.run([sys.executable, str(SCRIPT), "--config", str(cfg), str(mem), "--dry-run"],
                   check=True, capture_output=True)
    assert not (data / "memory.db").exists()


def test_tag_without_its_own_id_is_not_misfiled(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("imp", SCRIPT)
    imp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imp)
    optin = tmp_path / "optin.list"
    optin.write_text("ID 01AAAAAAAAAAAAAAAAAAAAAAA1\nTAG alice#1001\nTAG orphan#2002\n"
                     "ID 01BBBBBBBBBBBBBBBBBBBBBBB2\nID 01CCCCCCCCCCCCCCCCCCCCCCC3\nTAG ambiguous#3003\n"
                     "ID 01DDDDDDDDDDDDDDDDDDDDDDD4\nTAG dana#4004\n")
    m = imp.tag_to_id(optin, None)
    assert m == {"alice#1001": "01AAAAAAAAAAAAAAAAAAAAAAA1", "dana#4004": "01DDDDDDDDDDDDDDDDDDDDDDD4"}
