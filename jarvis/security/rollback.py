"""Reversibility manager (PLANv3 / PLANv2 §13.6).

Deletes go to the Recycle Bin; overwrites snapshot prior content; bulk moves
record an undo manifest. Each reversible action returns a token; rollback uses
it to restore. Snapshots live under the artifacts dir.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("jarvis.security.rollback")

try:
    from send2trash import send2trash  # type: ignore
except Exception:  # pragma: no cover - optional dep
    send2trash = None


@dataclass
class RollbackRecord:
    token: str
    kind: str  # snapshot | move_manifest | trash
    target: str
    snapshot_path: str | None = None
    manifest: list[dict[str, str]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    restored: bool = False


class RollbackManager:
    def __init__(
        self,
        snapshot_dir: Path,
        persist: Callable[[RollbackRecord], None] | None = None,
        store=None,
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, RollbackRecord] = {}
        self._persist = persist
        self._db = store  # optional jarvis.data.Store for durable load/lookup
        # NOTE: attribute is _db (not _store) to avoid clobbering the _store() method.

    def load(self) -> int:
        """Hydrate in-memory records from the store so rollback works across restarts."""
        if self._db is None:
            return 0
        n = 0
        for row in self._db.list_rollbacks():
            rec = self._row_to_record(row)
            self._records[rec.token] = rec
            n += 1
        return n

    @staticmethod
    def _row_to_record(row: dict) -> RollbackRecord:
        manifest = row.get("manifest_json") or row.get("manifest") or "[]"
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except Exception:
                manifest = []
        return RollbackRecord(
            token=row["token"],
            kind=row["kind"],
            target=row["target"],
            snapshot_path=row.get("snapshot_path"),
            manifest=manifest if isinstance(manifest, list) else [],
            created_at=row.get("created_at") or time.time(),
            restored=bool(row.get("restored_at")),
        )

    def _store(self, rec: RollbackRecord) -> str:
        self._records[rec.token] = rec
        if self._persist:
            try:
                self._persist(rec)
            except Exception:
                pass
        return rec.token

    def snapshot_file(self, path: str | Path) -> str | None:
        """Copy a file's current content aside before it is overwritten."""
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        token = f"rb_{uuid.uuid4().hex[:12]}"
        dest = self.snapshot_dir / f"{token}{p.suffix}"
        shutil.copy2(p, dest)
        return self._store(RollbackRecord(token=token, kind="snapshot", target=str(p), snapshot_path=str(dest)))

    def record_moves(self, moves: list[tuple[str, str]]) -> str:
        token = f"rb_{uuid.uuid4().hex[:12]}"
        manifest = [{"src": s, "dst": d} for s, d in moves]
        return self._store(RollbackRecord(token=token, kind="move_manifest", target="bulk_move", manifest=manifest))

    def trash(self, path: str | Path) -> str | None:
        """Delete reversibly: snapshot a copy, then send the original to the Recycle
        Bin (or remove it if send2trash is unavailable). The snapshot lets rollback
        restore the file/dir on any OS, even though Recycle Bin restore is manual."""
        p = Path(path)
        if not p.exists():
            return None
        token = f"rb_{uuid.uuid4().hex[:12]}"
        if p.is_dir():
            snap = self.snapshot_dir / f"{token}_{p.name}"
            shutil.copytree(p, snap)
        else:
            snap = self.snapshot_dir / f"{token}{p.suffix}"
            shutil.copy2(p, snap)
        # remove the original
        if send2trash is not None:
            send2trash(str(p))
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return self._store(RollbackRecord(token=token, kind="trash", target=str(p), snapshot_path=str(snap)))

    def rollback(self, token: str) -> bool:
        rec = self._records.get(token)
        if rec is None and self._db is not None:  # durable lookup across restarts
            row = self._db.get_rollback(token)
            if row:
                rec = self._row_to_record(row)
                self._records[token] = rec
        if rec is None or rec.restored:
            return False
        if rec.snapshot_path:  # covers both overwrite snapshots and reversible deletes
            src, tgt = Path(rec.snapshot_path), Path(rec.target)
            if src.is_dir():
                shutil.copytree(src, tgt, dirs_exist_ok=True)
            else:
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tgt)
        elif rec.kind == "move_manifest":
            for m in reversed(rec.manifest):
                src, dst = Path(m["src"]), Path(m["dst"])
                if dst.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
        else:
            return False
        rec.restored = True
        if self._db is not None:
            try:
                self._db.mark_rollback_restored(token)
            except Exception:
                logger.warning("failed to persist rollback restore for %s", token, exc_info=True)
        return True

    def get(self, token: str) -> RollbackRecord | None:
        return self._records.get(token)
