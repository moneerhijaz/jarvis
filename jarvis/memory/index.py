"""Derived note index over the vault (ported idea from gamma/prod).

The Markdown vault stays canonical. This keeps a rebuildable SQLite catalog of
notes (with a content hash for change detection) and the wikilink edges, so
keyword search and graph traversal are fast indexed queries instead of a full
filesystem rescan. Safe to delete and rebuild via ``reindex_all``.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from jarvis.memory.vault import Note, file_hash, slugify

_WORD = re.compile(r"[a-z0-9]+")

if TYPE_CHECKING:  # avoid import cycle at runtime
    from jarvis.data.store import Store
    from jarvis.memory.vault import Vault


def _summary(body: str, limit: int = 240) -> str:
    compact = " ".join(body.split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


class VaultIndex:
    def __init__(self, store: "Store", vault: "Vault") -> None:
        self.store = store
        self.vault = vault

    # -- writes ------------------------------------------------------------ #
    def sync_note(self, note: Note) -> None:
        rel = self.vault.rel(note.path)
        body = note.body
        self.store.upsert_memory_note({
            "vault_path": rel,
            "id": note.meta.get("id"),
            "type": note.meta.get("type", "note"),
            "title": note.title,
            "project_id": _project_of(rel),
            "source": note.meta.get("source"),
            "source_uri": note.meta.get("source_uri"),
            "created_at": None,
            "updated_at": None,
            "content_hash": file_hash(body),
            "frontmatter": note.meta,
            "summary": _summary(body),
            "search_text": (note.title + "\n" + body)[:4000],
        })
        self.store.replace_links(rel, note.links)

    def needs_reindex(self, rel: str, body: str) -> bool:
        row = self.store.get_memory_note(rel)
        return row is None or row.get("content_hash") != file_hash(body)

    def forget(self, rel: str) -> None:
        self.store.mark_memory_note_deleted(rel)

    def reindex_all(self) -> int:
        """Rebuild the catalog + links from the canonical Markdown."""
        live = {self.vault.rel(n.path) for n in self.vault.all_notes()}
        n = 0
        for note in self.vault.all_notes():
            self.sync_note(note)
            n += 1
        # prune catalog entries whose file no longer exists
        for row in self.store.list_memory_notes():
            if row["vault_path"] not in live:
                self.store.mark_memory_note_deleted(row["vault_path"])
        return n

    # -- reads ------------------------------------------------------------- #
    def search(self, query: str, project: str | None = None, limit: int = 20) -> list[dict]:
        pid = slugify(project) if project else None
        terms = [t for t in _WORD.findall(query.lower()) if len(t) > 2] or [query]
        out: dict[str, dict] = {}
        for term in terms:  # OR across terms for recall (LIKE is substring-only per term)
            for row in self.store.search_memory_notes(term, project_id=pid, limit=limit):
                out.setdefault(row["vault_path"], row)
        return list(out.values())[: limit * 2]

    def neighbors(self, rel: str, hops: int = 1) -> set[str]:
        """Graph traversal via persisted edges (link_text resolved by title/stem slug)."""
        # Build a slug -> path map once from the catalog.
        slug_to_path: dict[str, str] = {}
        for row in self.store.list_memory_notes():
            p = row["vault_path"]
            title = row.get("title") or ""
            slug_to_path.setdefault(slugify(title), p)
            stem = p.rsplit("/", 1)[-1][:-3] if p.endswith(".md") else p
            slug_to_path.setdefault(slugify(stem), p)

        seen: set[str] = set()
        frontier = {rel}
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for node in frontier:
                for lt in self.store.links_from(node):
                    key = slugify(lt.split("|")[0].split("#")[0])
                    tgt = slug_to_path.get(key)
                    if tgt and tgt not in seen:
                        seen.add(tgt)
                        nxt.add(tgt)
            frontier = nxt
        seen.discard(rel)
        return seen


def _project_of(rel: str) -> str | None:
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] == "projects":
        return parts[1]
    return None
