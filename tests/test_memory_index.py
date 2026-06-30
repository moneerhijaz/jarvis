"""Tests for the ported SQLite note catalog + persisted wikilink graph."""
from jarvis.data.store import Store
from jarvis.memory.vault import Vault, file_hash
from jarvis.memory.index import VaultIndex
from jarvis.memory.retrieval import Retriever


def _vault_with_index(tmp_path):
    store = Store(tmp_path / "jarvis.sqlite")
    v = Vault(tmp_path / "brain")
    v.attach_index(VaultIndex(store, v))
    v.scaffold()
    return v, store


def test_write_note_populates_catalog(tmp_path):
    v, store = _vault_with_index(tmp_path)
    v.write_note("Coffee prefs", "The user likes oat milk lattes.", tags=["food"])
    row = store.get_memory_note("notes/coffee-prefs.md")
    assert row is not None
    assert row["title"] == "Coffee prefs"
    assert row["content_hash"]
    assert "oat milk" in (row["search_text"] or "")


def test_content_hash_change_detection(tmp_path):
    v, store = _vault_with_index(tmp_path)
    v.write_note("note1", "first version")
    assert not v.index.needs_reindex("notes/note1.md", "first version")
    assert v.index.needs_reindex("notes/note1.md", "changed body")


def test_persisted_links_and_neighbors(tmp_path):
    v, store = _vault_with_index(tmp_path)
    v.write_note("alpha", "see [[beta]]")
    v.write_note("beta", "see [[gamma]]")
    v.write_note("gamma", "leaf")
    nb = v.index.neighbors("notes/alpha.md", hops=2)
    assert "notes/beta.md" in nb
    assert "notes/gamma.md" in nb
    # edges are persisted in the DB
    assert any(e["from_path"] == "notes/alpha.md" for e in store.all_links())


def test_indexed_retrieval_used_when_catalog_present(tmp_path):
    v, store = _vault_with_index(tmp_path)
    v.write_note("coffee", "The user prefers oat milk lattes.")
    v.write_note("taxes", "Notes about quarterly taxes.")
    hits = Retriever(v, embed_fn=None).search("what milk for coffee")
    assert hits and hits[0].source == "notes/coffee.md"


def test_reindex_rebuilds_and_prunes(tmp_path):
    v, store = _vault_with_index(tmp_path)
    n = v.write_note("keepme", "important content")
    # simulate an external edit by deleting the file, then reindex prunes the catalog
    (v.root / "notes" / "keepme.md").unlink()
    v.index.reindex_all()
    assert store.get_memory_note("notes/keepme.md") is None


def test_project_scoped_indexed_search(tmp_path):
    v, store = _vault_with_index(tmp_path)
    v.new_project("YouTube Channel", goal="grow")
    v.write_note("idea1", "great video idea", folder="projects/youtube-channel/Inputs")
    v.write_note("other", "unrelated note")
    scoped = Retriever(v, embed_fn=None).search("idea", project="YouTube Channel")
    assert scoped and all("youtube-channel" in s.source for s in scoped)
