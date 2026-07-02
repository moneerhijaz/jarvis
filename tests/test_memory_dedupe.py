"""Phase 2 — dedupe-on-write: a repeat capture merges into the existing note (and counts as a
recurrence) instead of piling up duplicates. Uses a deterministic fake embedder (no model)."""
import hashlib

from jarvis.data.store import Store
from jarvis.memory.index import VaultIndex
from jarvis.memory.vault import Vault


def _fake_embed(texts):
    """Identical text -> identical one-hot vector (cosine 1.0); different text -> orthogonal (0.0).
    Lets us test the 0.92 dedupe threshold deterministically."""
    out = []
    for t in texts:
        h = int(hashlib.md5(t.strip().encode()).hexdigest(), 16) % 1024
        v = [0.0] * 1024
        v[h] = 1.0
        out.append(v)
    return out


def _vault(tmp_path):
    store = Store(tmp_path / "j.sqlite")
    vault = Vault(tmp_path / "brain")
    vault.scaffold()
    vault.attach_index(VaultIndex(store, vault, embed_fn=_fake_embed, embed_model="fake"))
    return store, vault


def _inbox(vault):
    return [n for n in vault.all_notes() if vault.rel(n.path).startswith("inbox/")]


def test_identical_capture_dedupes(tmp_path):
    store, vault = _vault(tmp_path)
    n1 = vault.capture("my project codename is BlueFalcon")
    n2 = vault.capture("my project codename is BlueFalcon")   # identical -> should merge
    assert vault.rel(n1.path) == vault.rel(n2.path)
    assert len(_inbox(vault)) == 1                            # no duplicate written
    # the merge is recorded as a recurrence (promotion signal for Phase 3)
    assert store.get_memory_note(vault.rel(n1.path))["access_count"] >= 1
    store.close()


def test_distinct_capture_creates_new(tmp_path):
    store, vault = _vault(tmp_path)
    vault.capture("my project codename is BlueFalcon")
    vault.capture("I strongly prefer tea over coffee in the morning")
    assert len(_inbox(vault)) == 2                            # genuinely different -> kept separate
    store.close()
