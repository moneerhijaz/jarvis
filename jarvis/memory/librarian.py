"""Memory v2 librarian — periodic consolidation of the vault's memory.

Deterministic pass (no model):
  1. merge near-duplicate notes (cosine ≥ threshold) — keep the richest, archive the rest with a
     ``superseded_by`` lineage, and roll their recurrence into the keeper;
  2. promote recurring memories (reused ≥ N times, e.g. via dedupe hits) from episodic to long-term;
  3. decay + archive stale, never-recalled episodic notes.

Never deletes — 'archive' is a recall-excluded tier and everything stays on disk. An optional LLM
grouping pass (entity/topic concept notes with descriptions) layers on top separately.
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger("jarvis.memory.librarian")

_STOP = set(
    "the a an and or of to in on for is are was were be been i my me you your it its this that with "
    "about their them they he she his her our we us as at by from into over under out up down off not "
    "no yes do does did have has had will would can could should may might must than then so if but "
    "what which who whom whose when where why how".split())


def _keyword_title(texts: list[str], k: int = 3) -> str:
    """Deterministic group title from the most frequent content words — no model, so it can't echo
    a prompt example or hallucinate ('date year' notes -> 'Date Year Current', never 'Beverages')."""
    counts: Counter = Counter()
    for t in texts:
        for w in re.findall(r"[a-z0-9]{3,}", (t or "").lower()):
            if w not in _STOP:
                counts[w] += 1
    top = [w for w, _ in counts.most_common(k)]
    return " ".join(w.capitalize() for w in top) or "Untitled group"


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(y * y for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


@dataclass
class LibrarianReport:
    merged: int = 0
    promoted: int = 0
    archived: int = 0

    def __str__(self) -> str:
        return f"merged={self.merged} promoted={self.promoted} archived={self.archived}"


class Librarian:
    def __init__(self, store, vault, *, dup_threshold: float = 0.92, promote_hits: int = 2,
                 stale_days: int = 30, embed_fn=None) -> None:
        self.store = store
        self.vault = vault
        self.dup_threshold = dup_threshold
        self.promote_hits = promote_hits
        self.stale_days = stale_days
        self.embed_fn = embed_fn

    def consolidate(self) -> LibrarianReport:
        rep = LibrarianReport()
        try:
            rep.merged = self._merge_duplicates()
            rep.promoted = self._promote_recurring()
            rep.archived = self._decay_and_archive()
        except Exception:
            logger.warning("librarian consolidation failed", exc_info=True)
        logger.info("librarian: %s", rep)
        return rep

    # -- helpers ---------------------------------------------------------- #
    def _active_notes(self) -> list[dict]:
        return [n for n in self.store.list_memory_notes()
                if (n.get("tier") or "medium") != "archive"
                and not n["vault_path"].startswith("daily/")]

    # -- 1) merge near-duplicates ----------------------------------------- #
    def _merge_duplicates(self) -> int:
        vecs = {v["vault_path"]: v["embedding"]
                for v in self.store.all_memory_vectors(exclude_archived=True)}
        notes = {n["vault_path"]: n for n in self._active_notes() if n["vault_path"] in vecs}
        paths = list(notes)
        seen: set[str] = set()
        merged = 0
        for i, p in enumerate(paths):
            if p in seen:
                continue
            cluster = [p]
            for q in paths[i + 1:]:
                if q not in seen and _cos(vecs[p], vecs[q]) >= self.dup_threshold:
                    cluster.append(q)
                    seen.add(q)
            if len(cluster) < 2:
                continue
            keeper = max(cluster, key=lambda x: (notes[x].get("confidence") or 0.0,
                                                 notes[x].get("updated_at") or 0.0))
            recurrence = 0
            for x in cluster:
                if x == keeper:
                    continue
                self.store.set_memory_tier(x, "archive", superseded_by=keeper)
                recurrence += (notes[x].get("access_count") or 0) + 1
                merged += 1
            for _ in range(recurrence):        # roll merged copies' recurrence into the keeper
                self.store.touch_memory_note(keeper)
        return merged

    # -- 2) promote recurring memories ------------------------------------ #
    def _promote_recurring(self) -> int:
        n = 0
        for note in self._active_notes():
            tier = note.get("tier") or "medium"
            if tier in ("medium", "short") and (note.get("access_count") or 0) >= self.promote_hits:
                self.store.set_memory_tier(note["vault_path"], "long")
                n += 1
        return n

    # -- clustering + grouping (concept notes with descriptions) ---------- #
    def cluster(self, threshold: float = 0.5, min_size: int = 2) -> list[list[str]]:
        """Greedy embedding clusters of related active notes (cosine ≥ threshold). Lower threshold
        than merge (0.92) — these are RELATED, not duplicate. Returns clusters of size ≥ min_size."""
        vecs = {v["vault_path"]: v["embedding"]
                for v in self.store.all_memory_vectors(exclude_archived=True)}
        active = [n["vault_path"] for n in self._active_notes() if n["vault_path"] in vecs]
        seen: set[str] = set()
        clusters: list[list[str]] = []
        for i, p in enumerate(active):
            if p in seen:
                continue
            group = [p]
            seen.add(p)
            for q in active[i + 1:]:
                if q not in seen and _cos(vecs[p], vecs[q]) >= threshold:
                    group.append(q)
                    seen.add(q)
            if len(group) >= min_size:
                clusters.append(group)
        return clusters

    def group_and_describe(self, label_fn=None, threshold: float = 0.5, min_size: int = 2) -> int:
        """Turn clusters of related memories into entity/topic CONCEPT NOTES.

        The TITLE is derived deterministically from keywords (no model — can't echo/hallucinate).
        ``label_fn(list[str]) -> {"kind","description"} | None`` is optional and only supplies the
        prose description + entity/topic kind; a bad or missing model just falls back to a keyword
        title and a snippet-based description. Records the group, stamps members' group_id, and
        writes a concept note (map-of-content)."""
        from jarvis.memory.vault import slugify
        made = 0
        for cluster in self.cluster(threshold, min_size):
            texts = [(self.store.get_memory_note(p) or {}).get("summary") or p for p in cluster]
            title = _keyword_title(texts)
            kind, description = "topic", ""
            if label_fn is not None:
                try:
                    lab = label_fn(texts) or {}
                    if isinstance(lab, dict):
                        if str(lab.get("kind", "")).lower() == "entity":
                            kind = "entity"
                        d = str(lab.get("description") or "").strip()
                        if d and "<" not in d:          # reject echoed placeholders/examples
                            description = d
                except Exception:
                    logger.warning("group description failed; using fallback", exc_info=True)
            if not description:
                description = "Related memories: " + "; ".join(texts[:3])[:240]
            gid = slugify(title) or f"group-{made + 1}"
            self.store.upsert_memory_group({"id": gid, "kind": kind, "title": title,
                                            "description": description, "tier": "long"})
            for p in cluster:
                cur = (self.store.get_memory_note(p) or {}).get("tier") or "medium"
                self.store.set_memory_tier(p, cur, group_id=gid)
            body = description + "\n\n## Related memories\n" + "\n".join(f"- {t}" for t in texts)
            self.vault.write_note(f"{title} (memory)", body,
                                  folder="projects" if kind == "entity" else "notes",
                                  type_="concept", source="librarian")
            made += 1
        return made

    # -- 3) decay + archive stale, unused episodic ------------------------ #
    def _decay_and_archive(self) -> int:
        now = time.time()
        n = 0
        for note in self._active_notes():
            tier = note.get("tier") or "medium"
            if tier not in ("medium", "short"):
                continue                       # long-term never decays out
            age = now - (note.get("updated_at") or now)
            if (note.get("access_count") or 0) == 0 and age > self.stale_days * 86400:
                self.store.set_memory_tier(note["vault_path"], "archive")
                n += 1
        return n
