"""The plain-text Markdown vault - JARVIS's canonical memory (PLANv3 Part A).

The vault is a folder of Markdown notes the user owns. Notes carry YAML
frontmatter and link via [[wikilinks]]. This module is the only writer/reader of
the vault structure; everything is plain text and Obsidian-compatible.

No hard dependency on python-frontmatter: a tiny self-contained parser is used so
the backend runs with a minimal install.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("jarvis.memory.vault")

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

ROOT_PROFILE = "JARVIS.md"
PROJECT_PROFILE = "PROJECT.md"

# Lean PARA-style taxonomy (a trimmed take on gamma's tree).
DEFAULT_DIRS = [
    "inbox", "notes", "people", "projects", "skills", "daily",
    "areas", "resources", "archive", "system/templates",
]
PROJECT_SUBDIRS = ["Inputs", "Process", "Outputs", "Feedback", "Decisions", "Skills"]


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


@dataclass
class Note:
    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.path.stem

    @property
    def links(self) -> list[str]:
        return WIKILINK_RE.findall(self.body)

    def to_markdown(self) -> str:
        fm = yaml.safe_dump(self.meta, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{fm}\n---\n\n{self.body.strip()}\n"


def parse_markdown(path: Path, text: str) -> Note:
    m = _FM_RE.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:
            meta = {}
        body = m.group(2)
    else:
        meta, body = {}, text
    if not isinstance(meta, dict):
        meta = {}
    return Note(path=path, meta=meta, body=body)


class Vault:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.index = None  # optional VaultIndex; set via attach_index()

    def attach_index(self, index) -> None:
        self.index = index

    # -- lifecycle --------------------------------------------------------- #
    def exists(self) -> bool:
        return (self.root / ROOT_PROFILE).exists()

    def scaffold(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for d in DEFAULT_DIRS:
            # parents=True so nested entries like "system/templates" work
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / ".jarvis").mkdir(parents=True, exist_ok=True)
        if not (self.root / ROOT_PROFILE).exists():
            self.write_profile(self._starter_profile())

    def _starter_profile(self) -> str:
        return (
            "# About me\n\n(Run the first-run interview to fill this in.)\n\n"
            "# Goals\n\n# How to talk to me\n\n# Strengths and weaknesses\n\n# Current projects\n"
        )

    # -- profile ----------------------------------------------------------- #
    def read_profile(self) -> str:
        p = self.root / ROOT_PROFILE
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write_profile(self, text: str) -> None:
        (self.root / ROOT_PROFILE).write_text(text, encoding="utf-8")

    def read_project_profile(self, project: str) -> str:
        p = self.root / "projects" / slugify(project) / PROJECT_PROFILE
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # -- notes ------------------------------------------------------------- #
    def note_path(self, title: str, folder: str = "notes") -> Path:
        return self.root / folder / f"{slugify(title)}.md"

    def write_note(
        self,
        title: str,
        body: str,
        *,
        folder: str = "notes",
        type_: str = "note",
        tags: list[str] | None = None,
        source: str = "manual",
        source_uri: str | None = None,
        links: list[str] | None = None,
    ) -> Note:
        path = self.note_path(title, folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        existing = self.read_note(path) if path.exists() else None
        created = existing.meta.get("created") if existing else now
        if links:
            link_md = " ".join(f"[[{l}]]" for l in links)
            if link_md not in body:
                body = f"{body}\n\nRelated: {link_md}"
        # ID is unique + stable: derived from the unique vault-relative path
        # (reused if the note already exists), avoiding title-slug collisions.
        rel = self.rel(path)
        note_id = (existing.meta.get("id") if existing else None) or slugify(
            rel[:-3] if rel.endswith(".md") else rel
        )
        meta = {
            "id": note_id,
            "type": type_,
            "title": title,
            "tags": tags or [],
            "created": created,
            "updated": now,
            "source": source,
        }
        if source_uri:
            meta["source_uri"] = source_uri
        note = Note(path=path, meta=meta, body=body)
        path.write_text(note.to_markdown(), encoding="utf-8")
        if self.index is not None:  # keep the derived catalog in sync
            try:
                self.index.sync_note(note)
            except Exception:
                logger.warning("vault index sync failed for %s", self.rel(path), exc_info=True)
        return note

    def capture(self, text: str, source: str = "manual") -> Note:
        """Fast path: drop a raw note into inbox/ for the gardener to file later.

        Memory v2: dedupe-on-write. If a near-identical note already exists, treat this as a
        recurrence (touch it — a promotion signal) and return it instead of writing a duplicate."""
        if self.index is not None:
            dup = self.index.find_duplicate(text)
            if dup:
                try:
                    self.index.store.touch_memory_note(dup)
                except Exception:
                    pass
                return self.read_note(dup)
        title = text.strip().splitlines()[0][:60] if text.strip() else "capture"
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return self.write_note(f"{ts}-{title}", text, folder="inbox", type_="note", source=source)

    def read_note(self, path: str | Path) -> Note:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        return parse_markdown(p, p.read_text(encoding="utf-8"))

    def all_notes(self) -> list[Note]:
        notes: list[Note] = []
        for p in self.root.rglob("*.md"):
            if ".jarvis" in p.parts:
                continue
            try:
                notes.append(self.read_note(p))
            except Exception:
                continue
        return notes

    def rel(self, path: Path) -> str:
        # Always POSIX-style ('/') so citations and graph keys are stable across OSes.
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return Path(path).as_posix()

    # -- projects ---------------------------------------------------------- #
    def new_project(self, name: str, goal: str = "", role: str = "") -> Path:
        slug = slugify(name)
        base = self.root / "projects" / slug
        for sub in PROJECT_SUBDIRS:
            (base / sub).mkdir(parents=True, exist_ok=True)
        profile = base / PROJECT_PROFILE
        if not profile.exists():
            profile.write_text(
                f"# {name}\n\n## What this is\n{goal or '(describe the project)'}\n\n"
                f"## The one goal\n{goal or '(state the single goal)'}\n\n"
                f"## JARVIS's role\n{role or '(describe how JARVIS helps)'}\n",
                encoding="utf-8",
            )
        return base

    def list_projects(self) -> list[str]:
        base = self.root / "projects"
        if not base.exists():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    # -- skills ------------------------------------------------------------ #
    def read_skill(self, name: str) -> Note | None:
        p = self.root / "skills" / f"{slugify(name)}.md"
        return self.read_note(p) if p.exists() else None

    def list_skills(self) -> list[str]:
        base = self.root / "skills"
        if not base.exists():
            return []
        return sorted(p.stem for p in base.glob("*.md"))


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
