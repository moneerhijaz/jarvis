"""Offline memory probe — no LM Studio needed. Captures a fact, then shows EXACTLY what the
vault retriever returns for recall-style queries, so we can see whether the captured note is
found and how it ranks against existing notes (e.g. a JARVIS profile).

Run from the repo root:
    .venv312\\Scripts\\python tests\\mem_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app import Application          # noqa: E402
from jarvis.config import load_settings     # noqa: E402

FACT = "my project codename is BlueFalcon"
QUERIES = [
    "what is my project codename?",
    "project codename",
    "BlueFalcon",
    # Paraphrase with NO keyword overlap — only semantic (embedding) recall can surface the note:
    "what's the secret name for the thing I'm building?",
]


def main() -> int:
    app = Application(load_settings())
    app.ensure_vault()
    print(f"vault: {app.settings.vault_path}")

    note = app.vault.capture(FACT, source="mem_probe")
    print(f"captured -> {app.vault.rel(note.path)}  (title={note.title!r})\n")

    # what competes: notes mentioning codename / jarvis / profile
    competing = []
    for n in app.vault.all_notes():
        blob = ((n.body or "") + " " + (getattr(n, "title", "") or "")).lower()
        if any(t in blob for t in ("codename", "jarvis", "profile", "bluefalcon")):
            competing.append(f"  - {app.vault.rel(n.path)}  title={getattr(n,'title','')!r}")
    print(f"notes mentioning codename/jarvis/profile ({len(competing)}):")
    print("\n".join(competing) or "  (none)")
    print()

    for q in QUERIES:
        print(f"=== search({q!r}) ===")
        try:
            hits = app.retriever.search(q, k=6) or []
        except Exception as e:
            print(f"  ERROR: {e}\n")
            continue
        if not hits:
            print("  (no hits)\n")
            continue
        for i, h in enumerate(hits, 1):
            src = getattr(h, "source", "?")
            score = getattr(h, "score", "?")
            text = " ".join((getattr(h, "text", "") or "").split())[:140]
            print(f"  {i}. [{score}] {src}\n     {text}")
        found = any("bluefalcon" in (getattr(h, "text", "") or "").lower() for h in hits)
        print(f"  -> BlueFalcon present in hits: {found}\n")

    app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
