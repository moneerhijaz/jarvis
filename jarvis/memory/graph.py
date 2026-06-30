"""Wikilink graph over the vault (PLANv3 A.6).

Builds an adjacency map from [[wikilinks]] so retrieval can traverse connected
notes, not just vector-similar ones. Link targets are matched to notes by title
or stem (slug-insensitive).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from jarvis.memory.vault import Note, Vault, slugify


@dataclass
class Graph:
    # node key = vault-relative path; edges by resolved target path
    adj: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    title_index: dict[str, str] = field(default_factory=dict)  # slug(title|stem) -> path

    def neighbors(self, path: str, hops: int = 1) -> set[str]:
        seen: set[str] = set()
        frontier = {path}
        for _ in range(max(0, hops)):
            nxt: set[str] = set()
            for node in frontier:
                for tgt in self.adj.get(node, set()):
                    if tgt not in seen:
                        seen.add(tgt)
                        nxt.add(tgt)
            frontier = nxt
        seen.discard(path)
        return seen

    def orphans(self) -> list[str]:
        linked = set()
        for targets in self.adj.values():
            linked |= targets
        return [n for n in self.title_index.values() if n not in linked and not self.adj.get(n)]


def build_graph(vault: Vault, notes: list[Note] | None = None) -> Graph:
    notes = notes if notes is not None else vault.all_notes()
    g = Graph()
    # Index notes by title and stem slug for link resolution.
    for n in notes:
        rel = vault.rel(n.path)
        g.title_index[slugify(n.title)] = rel
        g.title_index.setdefault(slugify(n.path.stem), rel)
    for n in notes:
        rel = vault.rel(n.path)
        for link in n.links:
            key = slugify(link.split("|")[0].split("#")[0])
            target = g.title_index.get(key)
            if target:
                g.adj[rel].add(target)
    return g
