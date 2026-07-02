"""Phase 2 — post-run auto-extraction: the runner pulls durable facts out of an exchange, writes
them to memory (dedup'd + embedded), tags metadata, and type-fast-paths them to long-term.

Uses a scripted fake model + fake embedder, so it's offline and deterministic.
"""
import asyncio
import hashlib
from types import SimpleNamespace

from jarvis.data.store import Store
from jarvis.events import EventSink
from jarvis.memory.index import VaultIndex
from jarvis.memory.vault import Vault
from jarvis.model.client import ModelResponse
from jarvis.pipeline.runner import PipelineRunner


def _fake_embed(texts):
    out = []
    for t in texts:
        h = int(hashlib.md5(t.strip().encode()).hexdigest(), 16) % 1024
        v = [0.0] * 1024
        v[h] = 1.0
        out.append(v)
    return out


class ExtractModel:
    provider = "fake"

    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, tools=None, **kw):
        return ModelResponse(content=self.payload, provider="fake", model="x")


def _settings():
    return SimpleNamespace(
        vault=SimpleNamespace(auto_extract=True, embed=SimpleNamespace(model="fake")),
        autonomy=SimpleNamespace(max_retries=3, verification="single", question_timeout_s=600,
                                 confirm_level="never", max_depth=2, agreement="facts", max_replans=2),
        models=SimpleNamespace(vision_base_url=""),
        limits=SimpleNamespace(max_run_steps=40, max_wall_clock_s=60))


def _runner(tmp_path, payload):
    store = Store(tmp_path / "j.sqlite")
    vault = Vault(tmp_path / "brain")
    vault.scaffold()
    vault.attach_index(VaultIndex(store, vault, embed_fn=_fake_embed, embed_model="fake"))
    runner = PipelineRunner(_settings(), ExtractModel(payload), SimpleNamespace(), None,
                            EventSink(), store=store, vault=vault)
    return runner, store, vault


def _inbox(vault):
    return [n for n in vault.all_notes() if vault.rel(n.path).startswith("inbox/")]


def test_extracts_and_fastpaths_durable_facts(tmp_path):
    payload = ('{"memories":['
               '{"text":"My project codename is BlueFalcon","type":"identity","confidence":0.95},'
               '{"text":"I prefer concise answers","type":"preference","confidence":0.9}]}')
    runner, store, vault = _runner(tmp_path, payload)

    asyncio.run(runner._extract_memories(
        "remember my codename is BlueFalcon and that I like concise answers", "Noted.", "run_x"))

    notes = _inbox(vault)
    bodies = " ".join(n.body for n in notes).lower()
    assert "bluefalcon" in bodies and "concise" in bodies          # both facts captured
    # identity/preference are fast-pathed to long-term; metadata recorded
    for n in notes:
        row = store.get_memory_note(vault.rel(n.path))
        assert row["tier"] == "long"
        assert row["source_run_id"] == "run_x" and row["confidence"] >= 0.9
    store.close()


def test_empty_extraction_writes_nothing(tmp_path):
    runner, store, vault = _runner(tmp_path, '{"memories":[]}')
    before = len(_inbox(vault))
    asyncio.run(runner._extract_memories("what is 2+2?", "4", "run_y"))
    assert len(_inbox(vault)) == before                            # nothing durable -> no notes
    store.close()
