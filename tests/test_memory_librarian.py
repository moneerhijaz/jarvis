"""Phase 3 — librarian deterministic consolidation: merge duplicates, roll recurrence into the
keeper, promote it to long-term; leave distinct facts alone. Offline (fake embedder)."""
import hashlib
import re

from jarvis.data.store import Store
from jarvis.memory.index import VaultIndex
from jarvis.memory.librarian import Librarian
from jarvis.memory.vault import Vault


def _fake_embed(texts):
    out = []
    for t in texts:
        h = int(hashlib.md5(t.strip().encode()).hexdigest(), 16) % 1024
        v = [0.0] * 1024
        v[h] = 1.0
        out.append(v)
    return out


def _bow_embed(texts):
    """Bag-of-words vector: notes sharing tokens get partial cosine (unlike the one-hot embedder),
    so 'related but not identical' clustering is representable in tests."""
    out = []
    for t in texts:
        v = [0.0] * 256
        for tok in re.findall(r"[a-z]+", t.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 256] += 1.0
        out.append(v)
    return out


def _vault(tmp_path, embed=_fake_embed):
    store = Store(tmp_path / "j.sqlite")
    vault = Vault(tmp_path / "brain")
    vault.scaffold()
    vault.attach_index(VaultIndex(store, vault, embed_fn=embed, embed_model="fake"))
    return store, vault


def _active_inbox(store):
    return [n for n in store.list_memory_notes()
            if (n.get("tier") or "medium") != "archive" and n["vault_path"].startswith("inbox/")]


def test_merges_duplicates_and_promotes_keeper(tmp_path):
    store, vault = _vault(tmp_path)
    # three notes with IDENTICAL body (pre-dedupe duplicates) but distinct filenames
    for t in ("dup1", "dup2", "dup3"):
        vault.write_note(t, "my project codename is BlueFalcon", folder="inbox", type_="note", source="test")
    # ...and one genuinely different fact
    vault.write_note("other", "I strongly prefer tea over coffee", folder="inbox", type_="note", source="test")

    rep = Librarian(store, vault, embed_fn=_fake_embed).consolidate()
    assert rep.merged == 2                       # 3 identical -> keep 1, archive 2

    active = _active_inbox(store)
    paths = sorted(n["vault_path"] for n in active)
    assert len(active) == 2                       # 1 keeper + the distinct fact
    keeper = next(n for n in active if "BlueFalcon".lower() in (n["search_text"] or "").lower())
    assert keeper["tier"] == "long"               # rolled recurrence promoted the keeper
    # archived duplicates carry lineage and are excluded from recall
    archived = [n for n in store.list_memory_notes() if (n.get("tier") or "") == "archive"
                and n["vault_path"].startswith("inbox/")]
    assert len(archived) == 2 and all(a["superseded_by"] for a in archived)
    store.close()


def test_promotes_on_recurrence_only(tmp_path):
    store, vault = _vault(tmp_path)
    vault.write_note("a", "some episodic note about the weather", folder="inbox", type_="note", source="t")
    rel = _active_inbox(store)[0]["vault_path"]
    assert store.get_memory_note(rel)["tier"] == "medium"
    # simulate reuse (recall hits) then consolidate
    store.touch_memory_note(rel)
    store.touch_memory_note(rel)
    Librarian(store, vault, embed_fn=_fake_embed).consolidate()
    assert store.get_memory_note(rel)["tier"] == "long"
    store.close()


def test_cluster_groups_related_only(tmp_path):
    store, vault = _vault(tmp_path, embed=_bow_embed)
    vault.write_note("t1", "I like tea", folder="inbox", type_="note", source="t")
    vault.write_note("t2", "I really enjoy tea", folder="inbox", type_="note", source="t")
    vault.write_note("c", "BlueFalcon is my project codename", folder="inbox", type_="note", source="t")
    clusters = Librarian(store, vault, embed_fn=_bow_embed).cluster(threshold=0.5, min_size=2)
    assert len(clusters) == 1 and len(clusters[0]) == 2      # the two tea notes; codename excluded
    store.close()


def test_group_and_describe_writes_concept_note(tmp_path):
    store, vault = _vault(tmp_path, embed=_bow_embed)
    vault.write_note("t1", "I like tea", folder="inbox", type_="note", source="t")
    vault.write_note("t2", "I really enjoy tea", folder="inbox", type_="note", source="t")
    lib = Librarian(store, vault, embed_fn=_bow_embed)
    # label_fn supplies only kind + description now; the title is derived deterministically.
    made = lib.group_and_describe(lambda texts: {"kind": "topic", "description": "User enjoys tea."})
    assert made == 1
    groups = store.list_memory_groups()
    g = groups[0]
    assert "tea" in g["title"].lower()                       # keyword-derived title, not the model's
    assert g["description"] == "User enjoys tea."
    assert len(store.group_members(g["id"])) == 2            # both members stamped with group_id
    assert any("(memory)" in (n.title or "") for n in vault.all_notes())  # concept note written
    store.close()


def test_group_falls_back_without_model(tmp_path):
    store, vault = _vault(tmp_path, embed=_bow_embed)
    vault.write_note("t1", "I like tea", folder="inbox", type_="note", source="t")
    vault.write_note("t2", "I really enjoy tea", folder="inbox", type_="note", source="t")
    # no label_fn at all -> deterministic title + snippet description, still groups
    made = Librarian(store, vault, embed_fn=_bow_embed).group_and_describe(None)
    assert made == 1
    g = store.list_memory_groups()[0]
    assert "tea" in g["title"].lower() and g["description"]
    store.close()
