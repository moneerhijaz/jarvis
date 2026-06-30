"""Hybrid retrieval over the vault (PLANv3 A.6).

Combines keyword scoring, wikilink graph expansion, and optional vector
similarity into one ranked list of cited snippets. Vector search is optional: if
no embedder is configured, retrieval still works on keyword + graph alone, which
keeps the backend runnable with a minimal install.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis.memory.graph import build_graph
from jarvis.memory.vault import Note, Vault

EmbedFn = Callable[[list[str]], list[list[float]]]
_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Snippet:
    source: str  # vault-relative path (citation)
    title: str
    text: str
    score: float
    reason: str = ""


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _excerpt(body: str, terms: set[str], limit: int = 280) -> str:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return body[:limit]
    best, best_hits = lines[0], -1
    for ln in lines:
        hits = sum(1 for t in terms if t in ln.lower())
        if hits > best_hits:
            best, best_hits = ln, hits
    return best[:limit]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class Retriever:
    def __init__(
        self,
        vault: Vault,
        embed_fn: EmbedFn | None = None,
        graph_hops: int = 2,
        max_snippets: int = 12,
    ) -> None:
        self.vault = vault
        self.embed_fn = embed_fn
        self.graph_hops = graph_hops
        self.max_snippets = max_snippets

    def _scoped_notes(self, project: str | None) -> list[Note]:
        notes = self.vault.all_notes()
        if not project:
            return notes
        from jarvis.memory.vault import slugify

        prefix = (Path("projects") / slugify(project)).as_posix()
        return [n for n in notes if self.vault.rel(n.path).startswith(prefix)]

    def search(self, query: str, project: str | None = None, k: int | None = None) -> list[Snippet]:
        k = k or self.max_snippets
        index = getattr(self.vault, "index", None)
        if index is not None:
            indexed = self._search_indexed(index, query, project, k)
            if indexed is not None:
                return indexed
        return self._search_filescan(query, project, k)

    def _search_indexed(self, index, query: str, project: str | None, k: int) -> list[Snippet] | None:
        """Fast path: candidate selection + graph via the SQLite catalog/edges.

        Returns None to fall back to the file scan if the catalog looks empty
        (e.g. not yet reindexed), so behavior degrades gracefully.
        """
        rows = index.search(query, project=project, limit=k * 3)
        if not rows:
            return None
        terms = set(_tokens(query))
        merged: dict[str, float] = {r["vault_path"]: float(len(rows) - i) for i, r in enumerate(rows)}
        meta = {r["vault_path"]: r for r in rows}

        # graph expansion via persisted edges
        for seed in list(merged)[:5]:
            for nb in index.neighbors(seed, hops=self.graph_hops):
                if nb not in merged:
                    row = self.vault_meta(index, nb)
                    if row:
                        meta[nb] = row
                        merged[nb] = 0.5

        # Optional vector rerank — only worth the embedding round-trips when there
        # are MORE candidates than we'll return (i.e. ranking actually matters). On
        # a small vault keyword+graph already covers it, so we skip the ~0.8s of
        # embedding calls entirely.
        if self.embed_fn is not None and len(merged) > self.max_snippets:
            try:
                cand = list(merged)
                qv = self.embed_fn([query])[0]
                texts = [meta[p].get("search_text") or meta[p].get("summary") or "" for p in cand]
                for p, e in zip(cand, self.embed_fn(texts)):
                    merged[p] += 3.0 * _cosine(qv, e)
            except Exception:
                pass

        ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:k]
        out: list[Snippet] = []
        for rel, score in ranked:
            row = meta.get(rel) or {}
            text = row.get("summary") or ""
            try:  # prefer a query-relevant excerpt from the live Markdown
                text = _excerpt(self.vault.read_note(rel).body, terms)
            except Exception:
                pass
            out.append(Snippet(source=rel, title=row.get("title") or rel, text=text, score=round(score, 3)))
        return out

    @staticmethod
    def vault_meta(index, rel: str) -> dict | None:
        return index.store.get_memory_note(rel)

    def _search_filescan(self, query: str, project: str | None = None, k: int = 12) -> list[Snippet]:
        notes = self._scoped_notes(project)
        if not notes:
            return []
        terms = set(_tokens(query))
        by_path = {self.vault.rel(n.path): n for n in notes}

        # 1) keyword scores
        kw: dict[str, float] = {}
        for rel, n in by_path.items():
            hay = _tokens(n.title + " " + n.body)
            if not hay:
                continue
            hits = sum(hay.count(t) for t in terms)
            title_hits = sum(1 for t in terms if t in n.title.lower())
            if hits or title_hits:
                kw[rel] = hits + 2.0 * title_hits

        # 2) vector scores (optional) — only worth embedding when the vault is big
        # enough that keyword+graph isn't sufficient. Avoids needless embedding calls
        # (which can evict a GPU-resident chat model) on small vaults.
        vec: dict[str, float] = {}
        if self.embed_fn is not None and len(by_path) > 40:
            try:
                qv = self.embed_fn([query])[0]
                texts = [(n.title + "\n" + n.body)[:1000] for n in by_path.values()]
                embs = self.embed_fn(texts)
                for (rel, _), e in zip(by_path.items(), embs):
                    vec[rel] = _cosine(qv, e)
            except Exception:
                vec = {}

        # 3) merge keyword + vector
        merged: dict[str, float] = {}
        for rel in by_path:
            score = 0.0
            reason = []
            if rel in kw:
                score += kw[rel]
                reason.append("keyword")
            if rel in vec and vec[rel] > 0.1:
                score += 3.0 * vec[rel]
                reason.append("vector")
            if score > 0:
                merged[rel] = score

        # 4) graph expansion: pull neighbors of top hits
        if merged:
            graph = build_graph(self.vault, notes)
            seeds = sorted(merged, key=merged.get, reverse=True)[:5]
            for seed in seeds:
                for nb in graph.neighbors(seed, hops=self.graph_hops):
                    if nb in by_path:
                        merged.setdefault(nb, 0.0)
                        merged[nb] += 0.5  # connectivity bonus

        ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:k]
        out: list[Snippet] = []
        for rel, score in ranked:
            n = by_path[rel]
            out.append(
                Snippet(
                    source=rel,
                    title=n.title,
                    text=_excerpt(n.body, terms),
                    score=round(score, 3),
                )
            )
        return out
