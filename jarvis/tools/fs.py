"""Filesystem tools (PLANv3 / PLANv2 §7.8, §20.3).

Reversible by default: overwrites snapshot prior content, deletes go to the
Recycle Bin (via the rollback manager), bulk moves record an undo manifest.
Relative paths resolve against the run's working directory.
"""
from __future__ import annotations

import hashlib
import heapq
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from jarvis.tools.base import Artifact, ToolContext, ToolError, ToolResult, tool

_MAX_INLINE = 65536


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute() and ctx.working_directory:
        p = Path(ctx.working_directory) / p
    return p


def _err(code: str, msg: str, category: str = "execution", retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(code=code, message=msg, category=category, retryable=retryable))


class ListArgs(BaseModel):
    path: str = Field(".", description="Directory to list.")
    recursive: bool = False
    max_entries: int = 500


@tool(name="fs.list", risk="read", timeout_s=30, always_on=True,
      capabilities=["files.list"], output_kind="list",
      output_schema={"entries": [{"name": "str", "path": "str", "type": "str", "size": "int"}]},
      examples=[{"request": "what's in my Downloads folder", "args": {"path": "C:\\Users\\me\\Downloads"}}])
def fs_list(args: ListArgs, ctx: ToolContext) -> ToolResult:
    """List a directory's entries (name, type, size, modified time)."""
    p = _resolve(ctx, args.path)
    if not p.exists():
        return _err("not_found", f"{p} does not exist", "not_found")
    if not p.is_dir():
        return _err("not_dir", f"{p} is not a directory")
    it = p.rglob("*") if args.recursive else p.iterdir()
    entries = []
    for child in it:
        try:
            st = child.stat()
            entries.append({
                "name": child.name, "path": str(child),
                "type": "dir" if child.is_dir() else "file",
                "size": st.st_size, "modified": st.st_mtime,
            })
        except OSError:
            continue
        if len(entries) >= args.max_entries:
            break
    return ToolResult(ok=True, data={"path": str(p), "count": len(entries), "entries": entries})


class ReadArgs(BaseModel):
    path: str
    max_bytes: int = _MAX_INLINE


@tool(name="fs.read", risk="read", timeout_s=30, always_on=True,
      capabilities=["files.read"], output_kind="prose",
      output_schema={"content": "str", "truncated": "bool"})
def fs_read(args: ReadArgs, ctx: ToolContext) -> ToolResult:
    """Read a UTF-8 text file. Large files are truncated with a note."""
    p = _resolve(ctx, args.path)
    if not p.exists():
        return _err("not_found", f"{p} does not exist", "not_found")
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return _err("read_failed", str(e))
    truncated = len(data.encode("utf-8")) > args.max_bytes
    return ToolResult(ok=True, data={"path": str(p), "content": data[: args.max_bytes], "truncated": truncated})


class WriteArgs(BaseModel):
    path: str
    content: str
    create_parents: bool = True


@tool(name="fs.write", risk="write", timeout_s=30, always_on=True,
      capabilities=["files.write"], output_kind="action")
def fs_write(args: WriteArgs, ctx: ToolContext) -> ToolResult:
    """Create or overwrite a text file. Snapshots prior content for rollback."""
    p = _resolve(ctx, args.path)
    if ctx.policy and ctx.policy.is_protected_path(str(p)):
        return _err("protected", f"{p} is a protected system path", "permission")
    token = None
    if p.exists() and ctx.rollback:
        token = ctx.rollback.snapshot_file(p)
    if args.create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args.content, encoding="utf-8")
    return ToolResult(
        ok=True,
        data={"path": str(p), "bytes": len(args.content.encode("utf-8"))},
        artifacts=[Artifact(path=str(p), type="file")],
        rollback_token=token,
    )


class AppendArgs(BaseModel):
    path: str
    content: str


@tool(name="fs.append", risk="write", timeout_s=30,
      capabilities=["files.write"], output_kind="action")
def fs_append(args: AppendArgs, ctx: ToolContext) -> ToolResult:
    """Append text to a file, creating it if missing."""
    p = _resolve(ctx, args.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(args.content)
    return ToolResult(ok=True, data={"path": str(p)})


class MkdirArgs(BaseModel):
    path: str


@tool(name="fs.mkdir", risk="write", timeout_s=15,
      capabilities=["files.mkdir"], output_kind="action")
def fs_mkdir(args: MkdirArgs, ctx: ToolContext) -> ToolResult:
    """Create a directory (and parents)."""
    p = _resolve(ctx, args.path)
    p.mkdir(parents=True, exist_ok=True)
    return ToolResult(ok=True, data={"path": str(p)})


class MoveArgs(BaseModel):
    src: str
    dst: str


@tool(name="fs.move", risk="write", timeout_s=30,
      capabilities=["files.move"], output_kind="action")
def fs_move(args: MoveArgs, ctx: ToolContext) -> ToolResult:
    """Move/rename a file or directory, recording an undo manifest."""
    src = _resolve(ctx, args.src)
    dst = _resolve(ctx, args.dst)
    if not src.exists():
        return _err("not_found", f"{src} does not exist", "not_found")
    if ctx.policy and ctx.policy.is_protected_path(str(dst)):
        return _err("protected", f"{dst} is protected", "permission")
    dst.parent.mkdir(parents=True, exist_ok=True)
    token = ctx.rollback.record_moves([(str(src), str(dst))]) if ctx.rollback else None
    src.rename(dst)
    return ToolResult(ok=True, data={"src": str(src), "dst": str(dst)}, rollback_token=token)


class DeleteArgs(BaseModel):
    path: str


@tool(name="fs.delete", risk="destructive", timeout_s=30,
      capabilities=["files.delete"], output_kind="action")
def fs_delete(args: DeleteArgs, ctx: ToolContext) -> ToolResult:
    """Delete a file by sending it to the Recycle Bin (reversible)."""
    p = _resolve(ctx, args.path)
    if not p.exists():
        return _err("not_found", f"{p} does not exist", "not_found")
    if ctx.policy and ctx.policy.is_protected_path(str(p)):
        return _err("protected", f"{p} is protected", "permission")
    token = ctx.rollback.trash(p) if ctx.rollback else None
    if token is None and p.exists():
        return _err("delete_failed", "no reversible delete mechanism available")
    return ToolResult(ok=True, data={"path": str(p), "method": "recycle_bin"}, rollback_token=token)


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


class SizeArgs(BaseModel):
    path: str = Field(".", description="File or directory to measure.")


@tool(name="fs.size", risk="read", timeout_s=120, always_on=True,
      capabilities=["disk.measure", "files.size"], output_kind="scalar",
      output_schema={"bytes": "int", "human": "str", "files": "int"},
      examples=[{"request": "how big is my Projects folder", "args": {"path": "C:\\Users\\me\\Projects"}}])
def fs_size(args: SizeArgs, ctx: ToolContext) -> ToolResult:
    """Total size of a file or directory, recursively. Returns bytes, a human-readable
    size, and the number of files. Use this for 'how big is this folder' questions."""
    p = _resolve(ctx, args.path)
    if not p.exists():
        return _err("not_found", f"{p} does not exist", "not_found")
    total = 0
    files = 0
    if p.is_file():
        total = p.stat().st_size
        files = 1
    else:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
                    files += 1
            except OSError:
                continue
    return ToolResult(ok=True, data={"path": str(p), "bytes": total, "human": _human(total), "files": files})


class LargestFilesArgs(BaseModel):
    root: str = Field("C:\\", description="Directory to search recursively (default = system drive).")
    top: int = Field(5, description="How many of the largest files to return.")


@tool(name="fs.largest_files", risk="read", timeout_s=900,
      capabilities=["files.largest", "files.enumerate", "disk.measure"], output_kind="ranking",
      output_schema={"largest_files": [{"path": "str", "bytes": "int", "human": "str"}]},
      examples=[{"request": "the 5 largest files on my PC", "args": {"root": "C:\\", "top": 5}},
                {"request": "biggest files in my Videos folder", "args": {"root": "C:\\Users\\me\\Videos", "top": 10}}])
def fs_largest_files(args: LargestFilesArgs, ctx: ToolContext) -> ToolResult:
    """Recursively find the N largest individual FILES under a directory (not folders), with
    their full paths and sizes, largest first. Use this for 'the biggest/largest files on my
    PC / in this folder'. Walks the whole tree; unreadable folders are skipped."""
    root = _resolve(ctx, args.root)
    if not root.exists():
        return _err("not_found", f"{root} does not exist", "not_found")
    n = max(1, args.top)
    heap: list[tuple[int, str]] = []     # min-heap of the n largest (size, path)
    scanned = 0
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            sz = e.stat(follow_symlinks=False).st_size
                            scanned += 1
                            if len(heap) < n:
                                heapq.heappush(heap, (sz, e.path))
                            elif sz > heap[0][0]:
                                heapq.heapreplace(heap, (sz, e.path))
                    except (PermissionError, OSError):
                        pass
        except (PermissionError, OSError):
            pass
    ranked = sorted(heap, key=lambda x: x[0], reverse=True)
    files = [{"path": p, "bytes": s, "human": _human(s)} for s, p in ranked]
    return ToolResult(ok=True, redact_data=True,
                      data={"root": str(root), "files_scanned": scanned, "largest_files": files})


class SearchArgs(BaseModel):
    root: str = "."
    query: str = Field(..., description="Substring to find in file contents.")
    glob: str = "**/*"
    max_results: int = 100


@tool(name="fs.search", risk="read", timeout_s=60, always_on=True,
      capabilities=["files.search"], output_kind="list",
      output_schema={"matches": [{"path": "str", "snippet": "str"}]})
def fs_search(args: SearchArgs, ctx: ToolContext) -> ToolResult:
    """Search file contents under a root for a substring; returns paths + snippets."""
    root = _resolve(ctx, args.root)
    if not root.exists():
        return _err("not_found", f"{root} does not exist", "not_found")
    matches = []
    for f in root.glob(args.glob):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        idx = text.lower().find(args.query.lower())
        if idx >= 0:
            matches.append({"path": str(f), "snippet": text[max(0, idx - 40): idx + 80]})
        if len(matches) >= args.max_results:
            break
    return ToolResult(ok=True, data={"count": len(matches), "matches": matches})
