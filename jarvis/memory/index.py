"""Derived note index over the vault (ported idea from gamma/prod).

The Markdown vault stays canonical. This keeps a rebuildable SQLite catalog of
notes (with a content hash for change detection) and the wikilink edges, so
keyword search and graph traversal are fast indexed queries instead of a full
filesystem rescan. Safe to delete and rebuild via ``reindex_all``.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from jarvis.memory.vault import Note, file_hash, slugify

logger = logging.getLogger("jarvis.memory.index")
_WORD = re.compile(r"[a-z0-9]+")

if TYPE_CHECKING:  # avoid import cycle at runtime
    from jarvis.data.store import Store
    from jarvis.memory.vault import Vault


def _summary(body: str, limit: int = 240) -> str:
    compact = " ".join(body.split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(y * y for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


class VaultIndex:
    def __init__(self, store: "Store", vault: "Vault", embed_fn=None, embed_model: str = "") -> None:
        self.store = store
        self.vault = vault
        self.embed_fn = embed_fn          # Memory v2: callable(list[str]) -> list[vector]; may be None
        self.embed_model = embed_model or "embed"

    # -- writes ------------------------------------------------------------ #
    def sync_note(self, note: Note) -> None:
        rel = self.vault.rel(note.path)
        body = note.body
        new_hash = file_hash(body)
        prev = self.store.get_memory_note(rel)
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
            "content_hash": new_hash,
            "frontmatter": note.meta,
            "summary": _summary(body),
            "search_text": (note.title + "\n" + body)[:4000],
        })
        self.store.replace_links(rel, note.links)
        # Memory v2: (re)embed only when content changed or no vector exists yet — keeps reindex cheap.
        if self.embed_fn is not None:
            changed = prev is None or prev.get("content_hash") != new_hash
            if changed or self.store.get_memory_vector(rel) is None:
                self._embed_note(rel, note.title, body)

    def _embed_note(self, rel: str, title: str, body: str) -> None:
        try:
            # Embed the CONTENT (body), not the title — capture titles carry a timestamp prefix that
            # is pure noise and breaks dedupe (find_duplicate compares against the raw text/body).
            text = (body or "").strip() or (title or "")
            vec = self.embed_fn([text[:4000]])[0]
            vec = [float(x) for x in vec]
            self.store.set_memory_vector(rel, vec, model=self.embed_model, dim=len(vec))
        except Exception:
            logger.warning("embedding failed for %s", rel, exc_info=True)

    def find_duplicate(self, text: str, threshold: float = 0.92) -> str | None:
        """Return the vault path of an existing note whose embedding is near-identical to ``text``
        (cosine ≥ threshold), or None. Used to merge repeat captures instead of piling up copies."""
        if self.embed_fn is None or not text.strip():
            return None
        try:
            qv = [float(x) for x in self.embed_fn([text[:4000]])[0]]
        except Exception:
            return None
        best, best_sim = None, 0.0
        for row in self.store.all_memory_vectors(exclude_archived=True):
            sim = _cos(qv, row["embedding"])
            if sim > best_sim:
                best, best_sim = row["vault_path"], sim
        return best if best_sim >= threshold else None

    def reembed_all(self) -> int:
        """Recompute EVERY note's vector with the current representation (body-based). One-time
        migration when the embedding text changed — otherwise old title+body vectors linger and
        identical notes fail to match for dedupe/merge."""
        if self.embed_fn is None:
            return 0
        n = 0
        for row in self.store.list_memory_notes():
            rel = row["vault_path"]
            if rel.startswith("daily/"):
                continue
            try:
                note = self.vault.read_note(rel)
            except Exception:
                continue
            self._embed_note(rel, note.title, note.body)
            n += 1
        return n

    def backfill_vectors(self) -> int:
        """Embed catalog notes that have no stored vector yet (one-time after enabling embeddings)."""
        if self.embed_fn is None:
            return 0
        n = 0
        for rel in self.store.paths_without_vectors():
            if rel.startswith("daily/"):          # operational logs aren't recallable memory
                continue
            try:
                note = self.vault.read_note(rel)
            except Exception:
                continue
            self._embed_note(rel, note.title, note.body)
            n += 1
        if n:
            logger.info("backfilled embeddings for %d notes", n)
        return n

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
        rows: dict[str, dict] = {}
        matched: dict[str, int] = {}     # how many DISTINCT query terms each note matched
        for term in terms:               # OR across terms (LIKE is substring-only per term)
            for row in self.store.search_memory_notes(term, project_id=pid, limit=limit * 3):
                vp = row["vault_path"]
                # Skip operational logs (auto run-summaries / garden reports under daily/): they
                # are machine output, not user knowledge, and otherwise flood recall with hits on
                # common words like "what"/"project".
                if vp.startswith("daily/"):
                    continue
                rows.setdefault(vp, row)
                matched[vp] = matched.get(vp, 0) + 1
        ql = query.lower()

        def _score(vp: str) -> tuple:
            row = rows[vp]
            blob = ((row.get("search_text") or "") + " " + (row.get("title") or "")).lower()
            # rank: whole-query substring first, then MOST distinct query terms matched, then
            # recency — so "…project codename is BlueFalcon" (2 rare terms) beats a note matching
            # only a common word, instead of ordering purely by recency of the first term.
            return (ql in blob, matched[vp], row.get("updated_at") or 0)

        return [rows[vp] for vp in sorted(rows, key=_score, reverse=True)[: limit * 2]]

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
