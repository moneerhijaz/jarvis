"""Run the librarian's GROUPING pass on the real vault: cluster related memories and write
entity/topic concept notes with LLM-written descriptions. Needs LM Studio up (for the labels);
embeddings are local. Creates concept notes in your vault (never deletes anything).

    .venv312\\Scripts\\python tests\\mem_group.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app import Application              # noqa: E402
from jarvis.config import load_settings         # noqa: E402
from jarvis.memory.librarian import Librarian   # noqa: E402
from jarvis.model.client import Message         # noqa: E402
from jarvis.pipeline.runner import _extract_json  # noqa: E402


def main() -> int:
    app = Application(load_settings())
    app.ensure_vault()
    if app._embedder is None:
        print("No embedder configured (set vault.embed.mode: in_process).")
        return 1

    def label_fn(texts):
        sysp = ("You organize a personal memory vault. Name the group these related snippets form. "
                "Reply with ONLY a JSON object filled with REAL values for THESE snippets, e.g.: "
                '{"kind":"topic","title":"Beverage preferences","description":"Drinks the user '
                'likes."} — do NOT copy that example. Use "kind":"entity" only if they are all about '
                'ONE specific person/project/tool, otherwise "topic". /no_think')
        resp = app.model.chat([Message(role="system", content=sysp),
                               Message(role="user", content="Snippets:\n" + "\n".join(f"- {t}" for t in texts))],
                              temperature=0.0, max_tokens=200)
        return _extract_json(resp.content or "")

    # 0.72: tight enough that unrelated short notes don't chain into one blob (0.55 did).
    made = Librarian(app.store, app.vault, embed_fn=app._embedder).group_and_describe(
        label_fn, threshold=0.72, min_size=2)
    print(f"created/updated {made} concept group(s)\n")
    for g in app.store.list_memory_groups():
        members = app.store.group_members(g["id"])
        print(f"[{g['kind']}] {g['title']} — {g['description']}")
        for m in members:
            print(f"    • {m['vault_path']}")
    app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
