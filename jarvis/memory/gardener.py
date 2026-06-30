"""Vault gardener (PLANv3 A.10).

Files inbox notes into notes/, repairs/keeps wikilinks, flags stale or orphan
notes, and writes a short 'what changed' summary into daily/. Designed to run as
a scheduled autonomous task; here it's a callable the scheduler invokes.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from jarvis.memory.graph import build_graph
from jarvis.memory.vault import Vault

logger = logging.getLogger("jarvis.memory.gardener")


@dataclass
class GardenReport:
    filed: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    summary: str = ""


def _is_stale(note_updated: str | None, days: int = 30) -> bool:
    if not note_updated:
        return False
    try:
        t = time.mktime(time.strptime(note_updated, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return False
    return (time.time() - t) > days * 86400


def run_gardener(vault: Vault, stale_days: int = 30) -> GardenReport:
    report = GardenReport()

    # 1) File inbox items into notes/ (move = rewrite under notes/).
    inbox = vault.root / "inbox"
    if inbox.exists():
        for p in inbox.glob("*.md"):
            note = vault.read_note(p)
            vault.write_note(
                note.title,
                note.body,
                folder="notes",
                type_=note.meta.get("type", "note"),
                tags=note.meta.get("tags", []),
                source=note.meta.get("source", "inbox"),
            )
            p.unlink()
            report.filed.append(note.title)

    notes = vault.all_notes()

    # 2) Flag stale notes.
    for n in notes:
        if _is_stale(n.meta.get("updated"), stale_days):
            report.stale.append(vault.rel(n.path))

    # 3) Flag orphans (no inbound/outbound links).
    graph = build_graph(vault, notes)
    report.orphans = [
        o for o in graph.orphans() if not o.endswith("JARVIS.md") and "/PROJECT.md" not in o
    ]

    # 4) Write a daily summary note.
    day = time.strftime("%Y-%m-%d", time.gmtime())
    summary = (
        f"Filed {len(report.filed)} inbox note(s); "
        f"{len(report.stale)} stale; {len(report.orphans)} orphan(s)."
    )
    report.summary = summary
    vault.write_note(
        f"{day}-garden",
        f"# Garden report {day}\n\n{summary}\n\n"
        + ("## Filed\n" + "\n".join(f"- {x}" for x in report.filed) + "\n\n" if report.filed else "")
        + ("## Stale\n" + "\n".join(f"- {x}" for x in report.stale) + "\n\n" if report.stale else "")
        + ("## Orphans\n" + "\n".join(f"- {x}" for x in report.orphans) if report.orphans else ""),
        folder="daily",
        type_="daily",
        source="gardener",
    )
    # refresh the derived catalog/edges after filing + summarizing
    if getattr(vault, "index", None) is not None:
        try:
            vault.index.reindex_all()
        except Exception:
            logger.warning("gardener reindex failed", exc_info=True)
    return report
