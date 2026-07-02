"""Phase 0 (Memory v2) — store schema migration + vector/tier/group accessors.

Pure sqlite, no model or pydantic-heavy deps, so it runs fast and offline.
"""
import sqlite3

from jarvis.data.store import Store


def _note(store, path, **kw):
    row = {"vault_path": path, "title": kw.get("title", path), "search_text": kw.get("search_text", "")}
    row.update(kw)
    store.upsert_memory_note(row)


def test_fresh_db_has_v2_columns_with_defaults(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    assert store.schema_version == 3
    _note(store, "notes/a.md", title="A", search_text="hello world")
    n = store.get_memory_note("notes/a.md")
    assert n["tier"] == "medium"          # column default applied
    assert n["salience"] == 1.0
    assert n["access_count"] == 0
    assert n["confidence"] == 0.7
    store.close()


def test_migrates_preexisting_db(tmp_path):
    # Build a v2-era memory_notes table WITHOUT the Memory-v2 columns, then open Store.
    p = tmp_path / "old.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        """CREATE TABLE schema_info (version INTEGER NOT NULL);
           INSERT INTO schema_info(version) VALUES (2);
           CREATE TABLE memory_notes (
               vault_path TEXT PRIMARY KEY, id TEXT, type TEXT, title TEXT, project_id TEXT,
               source TEXT, source_uri TEXT, created_at REAL, updated_at REAL,
               content_hash TEXT, frontmatter_json TEXT, summary TEXT, search_text TEXT, deleted_at REAL);
           INSERT INTO memory_notes(vault_path,title,search_text) VALUES ('notes/old.md','Old','legacy');"""
    )
    con.commit(); con.close()

    store = Store(p)                       # triggers additive migration
    assert store.schema_version == 3
    cols = {r["name"] for r in store._query("SELECT name FROM pragma_table_info('memory_notes')")}
    assert {"tier", "salience", "group_id", "source_run_id", "superseded_by"} <= cols
    old = store.get_memory_note("notes/old.md")
    assert old["title"] == "Old" and old["tier"] == "medium"   # preserved + defaulted
    store.close()


def test_vector_roundtrip_and_exclusions(tmp_path):
    store = Store(tmp_path / "v.sqlite")
    _note(store, "notes/a.md")
    _note(store, "daily/run-run-x.md")
    _note(store, "notes/b.md")
    store.set_memory_vector("notes/a.md", [0.1, 0.2, 0.3], model="test", dim=3)
    store.set_memory_vector("daily/run-run-x.md", [0.9, 0.9, 0.9], model="test", dim=3)
    store.set_memory_vector("notes/b.md", [0.0, 1.0, 0.0], model="test", dim=3)

    got = store.get_memory_vector("notes/a.md")
    assert got is not None and len(got) == 3 and abs(got[0] - 0.1) < 1e-5

    store.set_memory_tier("notes/b.md", "archive")
    paths = {v["vault_path"] for v in store.all_memory_vectors(exclude_archived=True)}
    assert paths == {"notes/a.md"}         # daily/ excluded, archived excluded
    store.close()


def test_tier_touch_and_archive_logs(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    _note(store, "notes/a.md")
    _note(store, "daily/run-run-1.md")
    _note(store, "daily/garden-2026.md")

    store.set_memory_tier("notes/a.md", "long", salience=2.0)
    store.touch_memory_note("notes/a.md")
    a = store.get_memory_note("notes/a.md")
    assert a["tier"] == "long" and a["salience"] == 2.0 and a["access_count"] == 1 and a["last_accessed"]

    n = store.archive_operational_logs()
    assert n == 2                          # both daily/run- and daily/garden-
    assert store.get_memory_note("daily/run-run-1.md")["tier"] == "archive"
    store.close()


def test_groups(tmp_path):
    store = Store(tmp_path / "g.sqlite")
    _note(store, "projects/bluefalcon.md", title="BlueFalcon")
    store.upsert_memory_group({"id": "g1", "kind": "entity", "title": "BlueFalcon",
                               "description": "Project codename.", "tier": "long", "centroid": [0.1, 0.2]})
    store.set_memory_tier("projects/bluefalcon.md", "long", group_id="g1")
    groups = store.list_memory_groups()
    assert any(g["id"] == "g1" and g["kind"] == "entity" for g in groups)
    members = store.group_members("g1")
    assert [m["vault_path"] for m in members] == ["projects/bluefalcon.md"]
    store.close()
