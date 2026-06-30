"""Vault memory tools (PLANv3 A.7) - replaces PLANv2's memory.* tools.

These are how the agent reads and writes the plain-text second brain. Writes go
through the Vault (frontmatter + wikilinks). Search is hybrid (keyword + graph +
optional vector) and returns vault-path citations. Secrets are redacted before
anything is written to the vault.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from jarvis.memory.gardener import run_gardener
from jarvis.security.redaction import redact
from jarvis.tools.base import Artifact, ToolContext, ToolError, ToolResult, tool

VAULT_SEARCH = "vault.search"


def _need_vault(ctx: ToolContext) -> ToolResult | None:
    if ctx.vault is None:
        return ToolResult(ok=False, error=ToolError(code="no_vault", message="vault not configured", category="dependency_missing"))
    return None


class CaptureArgs(BaseModel):
    text: str = Field(..., description="A fact, preference, or note to remember.")


@tool(name="vault.capture", risk="write", always_on=True, timeout_s=20,
      capabilities=["memory.write"], output_kind="action")
def vault_capture(args: CaptureArgs, ctx: ToolContext) -> ToolResult:
    """Quickly remember something: writes a note into the vault inbox."""
    if (e := _need_vault(ctx)):
        return e
    note = ctx.vault.capture(redact(args.text) or "", source=f"run:{ctx.run_id}")
    return ToolResult(ok=True, data={"path": ctx.vault.rel(note.path), "title": note.title},
                      artifacts=[Artifact(path=str(note.path), type="note")])


class WriteNoteArgs(BaseModel):
    title: str
    body: str
    folder: str = "notes"
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list, description="Titles of related notes to [[wikilink]].")


@tool(name="vault.write_note", risk="write", timeout_s=20,
      capabilities=["memory.write"], output_kind="action")
def vault_write_note(args: WriteNoteArgs, ctx: ToolContext) -> ToolResult:
    """Create or update a vault note with frontmatter and wikilinks."""
    if (e := _need_vault(ctx)):
        return e
    note = ctx.vault.write_note(
        args.title, redact(args.body) or "", folder=args.folder,
        tags=args.tags, links=args.links, source=f"run:{ctx.run_id}",
    )
    return ToolResult(ok=True, data={"path": ctx.vault.rel(note.path), "title": note.title},
                      artifacts=[Artifact(path=str(note.path), type="note")])


class ReadNoteArgs(BaseModel):
    path: str = Field(..., description="Vault-relative path, e.g. notes/foo.md")


@tool(name="vault.read_note", risk="read", timeout_s=20,
      capabilities=["memory.read"], output_kind="prose")
def vault_read_note(args: ReadNoteArgs, ctx: ToolContext) -> ToolResult:
    """Read a vault note by its vault-relative path."""
    if (e := _need_vault(ctx)):
        return e
    try:
        note = ctx.vault.read_note(args.path)
    except FileNotFoundError:
        return ToolResult(ok=False, error=ToolError(code="not_found", message=args.path, category="not_found"))
    return ToolResult(ok=True, data={"title": note.title, "meta": note.meta, "body": note.body, "links": note.links})


class SearchArgs(BaseModel):
    query: str
    project: str | None = Field(None, description="Restrict to a project folder.")
    k: int = 8


@tool(name=VAULT_SEARCH, risk="read", always_on=True, timeout_s=30,
      capabilities=["memory.search", "memory.recall"], output_kind="list")
def vault_search(args: SearchArgs, ctx: ToolContext) -> ToolResult:
    """Search memory (hybrid: keyword + wikilink graph + vector). Returns cited snippets."""
    if ctx.retriever is None:
        return ToolResult(ok=False, error=ToolError(code="no_retriever", message="retriever not configured", category="dependency_missing"))
    project = args.project or ctx.project
    snips = ctx.retriever.search(args.query, project=project, k=args.k)
    return ToolResult(ok=True, data={"results": [s.__dict__ for s in snips], "count": len(snips)})


class UpdateProfileArgs(BaseModel):
    content: str = Field(..., description="Full new contents of JARVIS.md.")


@tool(name="vault.update_profile", risk="write", timeout_s=20)
def vault_update_profile(args: UpdateProfileArgs, ctx: ToolContext) -> ToolResult:
    """Overwrite the root profile (JARVIS.md). Snapshots prior content for rollback."""
    if (e := _need_vault(ctx)):
        return e
    token = None
    profile_path = ctx.vault.root / "JARVIS.md"
    if profile_path.exists() and ctx.rollback:
        token = ctx.rollback.snapshot_file(profile_path)
    ctx.vault.write_profile(redact(args.content) or "")
    return ToolResult(ok=True, data={"path": "JARVIS.md"}, rollback_token=token)


class NewProjectArgs(BaseModel):
    name: str
    goal: str = ""
    role: str = ""


@tool(name="vault.new_project", risk="write", timeout_s=20)
def vault_new_project(args: NewProjectArgs, ctx: ToolContext) -> ToolResult:
    """Scaffold a project folder (Inputs/Process/Outputs/Feedback + PROJECT.md)."""
    if (e := _need_vault(ctx)):
        return e
    base = ctx.vault.new_project(args.name, args.goal, args.role)
    return ToolResult(ok=True, data={"path": ctx.vault.rel(base), "project": base.name})


class SkillArgs(BaseModel):
    name: str


@tool(name="vault.read_skill", risk="read", timeout_s=20)
def vault_read_skill(args: SkillArgs, ctx: ToolContext) -> ToolResult:
    """Load a Markdown skill (reusable workflow) by name so it can be executed."""
    if (e := _need_vault(ctx)):
        return e
    note = ctx.vault.read_skill(args.name)
    if note is None:
        return ToolResult(ok=False, error=ToolError(code="not_found", message=args.name, category="not_found"))
    return ToolResult(ok=True, data={"name": note.title, "meta": note.meta, "steps": note.body})


class EmptyArgs(BaseModel):
    pass


@tool(name="vault.list_skills", risk="read", timeout_s=20)
def vault_list_skills(args: EmptyArgs, ctx: ToolContext) -> ToolResult:
    """List available skills in the vault."""
    if (e := _need_vault(ctx)):
        return e
    return ToolResult(ok=True, data={"skills": ctx.vault.list_skills(), "projects": ctx.vault.list_projects()})


@tool(name="vault.reindex", risk="read", timeout_s=120)
def vault_reindex(args: EmptyArgs, ctx: ToolContext) -> ToolResult:
    """Rebuild the derived note catalog + wikilink edges from the canonical Markdown."""
    if (e := _need_vault(ctx)):
        return e
    if getattr(ctx.vault, "index", None) is not None:
        n = ctx.vault.index.reindex_all()
        return ToolResult(ok=True, data={"indexed": n})
    # fallback: legacy vault_files bookkeeping
    from jarvis.memory.vault import file_hash

    n = 0
    for note in ctx.vault.all_notes():
        if ctx.store:
            ctx.store.upsert_vault_file(
                ctx.vault.rel(note.path), file_hash(note.body), note.path.stat().st_mtime,
                note.meta.get("type", "note"), len(note.links),
            )
        n += 1
    return ToolResult(ok=True, data={"indexed": n})


@tool(name="vault.garden", risk="write", timeout_s=120)
def vault_garden(args: EmptyArgs, ctx: ToolContext) -> ToolResult:
    """Run vault maintenance: file inbox notes, flag stale/orphan notes, write a daily summary."""
    if (e := _need_vault(ctx)):
        return e
    report = run_gardener(ctx.vault)
    return ToolResult(ok=True, data={"filed": report.filed, "stale": report.stale, "orphans": report.orphans, "summary": report.summary})
