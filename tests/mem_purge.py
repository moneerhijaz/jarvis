"""Purge test-junk from the vault so memory reflects real facts. Archives operational-junk
captures (date/year extraction, bare years, screen descriptions, the jarvis_test note), removes
the librarian's derived concept notes, and clears groups. Archiving is REVERSIBLE (recall-excluded,
not deleted); only derived concept notes are removed (the librarian rebuilds them).

    .venv312\\Scripts\\python tests\\mem_purge.py           # preview
    .venv312\\Scripts\\python tests\\mem_purge.py --apply   # actually do it
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app import Application          # noqa: E402
from jarvis.config import load_settings     # noqa: E402

# Operational noise from earlier test runs — NOT real user memory.
JUNK = re.compile(
    r"\bextract\b.*\b(year|date)\b|\bcurrent (year|date)\b|^\s*20\d\d\s*$|"
    r"left side of the screen|youtube video|screen shows|jarvis[_ ]test|"
    r"run (the )?(shell command|date command)|date /t|get-date|date command",
    re.I)


def main(apply: bool) -> int:
    app = Application(load_settings())
    app.ensure_vault()
    store, vault = app.store, app.vault

    archive, remove = [], []
    for n in store.list_memory_notes():
        vp = n["vault_path"]
        if (n.get("tier") or "medium") == "archive" or vp.startswith("daily/"):
            continue
        if n.get("source") == "librarian" or "(memory)" in (n.get("title") or ""):
            remove.append(vp)                                   # derived concept note -> rebuildable
        elif JUNK.search(n.get("search_text") or ""):
            archive.append(vp)

    print(f"would ARCHIVE {len(archive)} junk capture(s):")
    for p in archive:
        print(f"   - {p}")
    print(f"would REMOVE {len(remove)} derived concept note(s):")
    for p in remove:
        print(f"   - {p}")

    if not apply:
        print("\n(preview only — re-run with --apply to perform it)")
        app.close()
        return 0

    for vp in archive:
        store.set_memory_tier(vp, "archive")
    for vp in remove:
        try:
            (vault.root / vp).unlink()
        except OSError:
            pass
        store.mark_memory_note_deleted(vp)
    store.clear_memory_groups()

    active = [n["vault_path"] for n in store.list_memory_notes()
              if (n.get("tier") or "medium") != "archive" and not n["vault_path"].startswith("daily/")]
    print(f"\nDone. {len(active)} active memory note(s) remain:")
    for p in active:
        print(f"   - {p}")
    app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
