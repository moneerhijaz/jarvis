"""Run the Memory v2 librarian on the REAL vault: merge duplicates, promote recurring facts to
long-term, archive stale/unused episodic notes. Nothing is deleted (archive = recall-excluded).
Offline — uses the configured CPU embedder, no LM Studio needed.

    .venv312\\Scripts\\python tests\\mem_consolidate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app import Application          # noqa: E402
from jarvis.config import load_settings     # noqa: E402
from jarvis.memory.librarian import Librarian  # noqa: E402


def _counts(store):
    tiers: dict[str, int] = {}
    for n in store.list_memory_notes():
        tiers[n.get("tier") or "medium"] = tiers.get(n.get("tier") or "medium", 0) + 1
    return tiers


def main() -> int:
    app = Application(load_settings())
    app.ensure_vault()                        # ensures every note is embedded first
    if app._embedder is None:
        print("No embedder configured (set vault.embed.mode: in_process) — merge needs vectors.")
        return 1

    # One-time migration: normalize every vector to the current body-based representation so
    # identical notes actually match (old title+body vectors carried timestamp noise).
    ren = app.vault.index.reembed_all()
    print(f"re-embedded {ren} notes (body-based)")
    print("tiers before:", _counts(app.store))
    rep = Librarian(app.store, app.vault, embed_fn=app._embedder).consolidate()
    print("librarian:", rep)
    print("tiers after: ", _counts(app.store))

    # Show what happened to the BlueFalcon notes specifically.
    print("\nBlueFalcon notes:")
    for n in app.store.list_memory_notes():
        if "bluefalcon" in (n.get("search_text") or "").lower():
            print(f"  [{n.get('tier')}] {n['vault_path']}"
                  + (f"  -> superseded_by {n['superseded_by']}" if n.get("superseded_by") else ""))
    app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
