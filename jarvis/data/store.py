"""SQLite operational store (PLANv3 §B.1 / PLANv2 §12).

Holds operational state only: threads, runs, messages, events, tool_calls,
artifacts, audit_entries, rollback_records, settings, vault_files. The vault
Markdown is the memory of record; this DB is rebuildable operational state.

Plain ``sqlite3`` (no ORM) keeps dependencies light. A single connection with a
lock is sufficient for a local single-operator backend.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, title TEXT, created_at REAL, updated_at REAL,
    archived INTEGER DEFAULT 0, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, thread_id TEXT, status TEXT, user_request TEXT,
    working_directory TEXT, project TEXT, model_provider TEXT, model_name TEXT,
    started_at REAL, ended_at REAL, summary TEXT, error_code TEXT, error_message TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, role TEXT, content TEXT,
    created_at REAL, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, run_id TEXT, thread_id TEXT, seq INTEGER, type TEXT,
    timestamp TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY, run_id TEXT, tool_name TEXT, arguments_json TEXT,
    result_json TEXT, status TEXT, started_at REAL, ended_at REAL,
    duration_ms INTEGER, error_code TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, run_id TEXT, tool_call_id TEXT, type TEXT, path TEXT,
    mime_type TEXT, size_bytes INTEGER, sha256 TEXT, created_at REAL, metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS audit_entries (
    id TEXT PRIMARY KEY, run_id TEXT, tool_call_id TEXT, action_type TEXT,
    target TEXT, summary TEXT, risk TEXT, arguments_redacted_json TEXT,
    result_summary_json TEXT, rollback_token TEXT, timestamp REAL
);
CREATE TABLE IF NOT EXISTS rollback_records (
    token TEXT PRIMARY KEY, run_id TEXT, tool_call_id TEXT, kind TEXT,
    target TEXT, snapshot_path TEXT, manifest_json TEXT, created_at REAL,
    restored_at REAL, status TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value_json TEXT, source TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS vault_files (
    path TEXT PRIMARY KEY, sha256 TEXT, mtime REAL, type TEXT,
    indexed_at REAL, link_count INTEGER
);
-- Indexed catalog of vault notes (Markdown is canonical; this is a rebuildable cache).
CREATE TABLE IF NOT EXISTS memory_notes (
    vault_path TEXT PRIMARY KEY, id TEXT, type TEXT, title TEXT, project_id TEXT,
    source TEXT, source_uri TEXT, created_at REAL, updated_at REAL,
    content_hash TEXT, frontmatter_json TEXT, summary TEXT, search_text TEXT,
    deleted_at REAL,
    -- Memory v2 (also added to pre-existing DBs via _add_missing_columns):
    tier TEXT DEFAULT 'medium', salience REAL DEFAULT 1.0, last_accessed REAL,
    access_count INTEGER DEFAULT 0, confidence REAL DEFAULT 0.7, group_id TEXT,
    source_run_id TEXT, superseded_by TEXT
);
-- Persisted wikilink edges for scalable graph traversal / orphan detection.
CREATE TABLE IF NOT EXISTS memory_links (
    from_path TEXT, link_text TEXT, created_at REAL,
    PRIMARY KEY (from_path, link_text)
);
-- Memory v2: per-note embedding vectors (semantic recall). Markdown stays canonical.
CREATE TABLE IF NOT EXISTS memory_vectors (
    vault_path TEXT PRIMARY KEY, embedding BLOB, model TEXT, dim INTEGER, updated_at REAL
);
-- Memory v2: entity/topic groups (concept notes / maps-of-content).
CREATE TABLE IF NOT EXISTS memory_groups (
    id TEXT PRIMARY KEY, kind TEXT, title TEXT, description TEXT, tier TEXT,
    centroid BLOB, created_at REAL, updated_at REAL
);

CREATE INDEX IF NOT EXISTS ix_events_run_seq ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_memory_notes_proj ON memory_notes(project_id);
CREATE INDEX IF NOT EXISTS ix_memory_links_from ON memory_links(from_path);
CREATE INDEX IF NOT EXISTS ix_runs_thread ON runs(thread_id);
CREATE INDEX IF NOT EXISTS ix_tool_calls_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS ix_audit_run ON audit_entries(run_id);
"""


def _j(v: Any) -> str:
    return json.dumps(v, default=str)


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    # Memory v2 columns added to memory_notes on pre-existing DBs (fresh DBs get them from _SCHEMA).
    _MEMORY_NOTE_COLUMNS = {
        "tier": "TEXT DEFAULT 'medium'",
        "salience": "REAL DEFAULT 1.0",
        "last_accessed": "REAL",
        "access_count": "INTEGER DEFAULT 0",
        "confidence": "REAL DEFAULT 0.7",
        "group_id": "TEXT",
        "source_run_id": "TEXT",
        "superseded_by": "TEXT",
    }

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Additive column migration for DBs created before Memory v2. ALTER TABLE ADD COLUMN
            # is the only safe schema change in SQLite and is idempotent here via the pragma check.
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(memory_notes)")}
            for col, decl in self._MEMORY_NOTE_COLUMNS.items():
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE memory_notes ADD COLUMN {col} {decl}")
            # Indexes that depend on the (possibly just-added) columns.
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_memory_notes_tier ON memory_notes(tier)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_memory_notes_group ON memory_notes(group_id)")
            cur = self._conn.execute("SELECT version FROM schema_info LIMIT 1")
            row = cur.fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] < SCHEMA_VERSION:
                self._conn.execute("UPDATE schema_info SET version=?", (SCHEMA_VERSION,))
            self._conn.commit()

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            return row["version"] if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- generic helpers --------------------------------------------------- #
    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # -- threads / runs ---------------------------------------------------- #
    def create_thread(self, thread_id: str, title: str = "") -> None:
        now = time.time()
        self._exec(
            "INSERT OR IGNORE INTO threads(id,title,created_at,updated_at,archived,metadata_json) VALUES (?,?,?,?,0,'{}')",
            (thread_id, title, now, now),
        )

    def list_threads(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM threads ORDER BY updated_at DESC")

    def create_run(self, run: dict[str, Any]) -> None:
        self._exec(
            """INSERT INTO runs(id,thread_id,status,user_request,working_directory,project,
                 model_provider,model_name,started_at,ended_at,summary,error_code,error_message)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["id"], run.get("thread_id"), run.get("status"), run.get("user_request"),
                run.get("working_directory"), run.get("project"), run.get("model_provider"),
                run.get("model_name"), run.get("started_at"), run.get("ended_at"),
                run.get("summary"), run.get("error_code"), run.get("error_message"),
            ),
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return rows[0] if rows else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))

    # -- messages (conversation continuity) ------------------------------- #
    def add_message(self, thread_id: str, run_id: str, role: str, content: str) -> None:
        self._exec(
            "INSERT INTO messages(id,thread_id,run_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,'{}')",
            (f"msg_{uuid.uuid4().hex[:12]}", thread_id, run_id, role, content, time.time()),
        )

    def list_messages(self, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at DESC LIMIT ?",
            (thread_id, limit),
        )
        return list(reversed(rows))  # chronological order

    # -- events ------------------------------------------------------------ #
    def add_event(self, ev: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO events(id,run_id,thread_id,seq,type,timestamp,payload_json) VALUES (?,?,?,?,?,?,?)",
            (ev["id"], ev["run_id"], ev.get("thread_id"), ev["seq"], ev["type"], ev["timestamp"], _j(ev.get("payload", {}))),
        )

    def get_events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq))

    # -- tool calls / audit / artifacts ----------------------------------- #
    def add_tool_call(self, tc: dict[str, Any]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO tool_calls(id,run_id,tool_name,arguments_json,result_json,
                 status,started_at,ended_at,duration_ms,error_code)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                tc["id"], tc["run_id"], tc["tool_name"], _j(tc.get("arguments", {})),
                _j(tc.get("result", {})), tc.get("status"), tc.get("started_at"),
                tc.get("ended_at"), tc.get("duration_ms"), tc.get("error_code"),
            ),
        )

    def add_audit(self, a: dict[str, Any]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO audit_entries(id,run_id,tool_call_id,action_type,target,
                 summary,risk,arguments_redacted_json,result_summary_json,rollback_token,timestamp)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                a["id"], a.get("run_id"), a.get("tool_call_id"), a.get("action_type"),
                a.get("target"), a.get("summary"), a.get("risk"),
                _j(a.get("arguments_redacted", {})), _j(a.get("result_summary", {})),
                a.get("rollback_token"), a.get("timestamp", time.time()),
            ),
        )

    def list_audit(self, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if run_id:
            return self._query("SELECT * FROM audit_entries WHERE run_id=? ORDER BY timestamp DESC LIMIT ?", (run_id, limit))
        return self._query("SELECT * FROM audit_entries ORDER BY timestamp DESC LIMIT ?", (limit,))

    def add_artifact(self, art: dict[str, Any]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO artifacts(id,run_id,tool_call_id,type,path,mime_type,
                 size_bytes,sha256,created_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                art["id"], art.get("run_id"), art.get("tool_call_id"), art.get("type"),
                art.get("path"), art.get("mime_type"), art.get("size_bytes"),
                art.get("sha256"), art.get("created_at", time.time()), _j(art.get("metadata", {})),
            ),
        )

    def get_rollback(self, token: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM rollback_records WHERE token=?", (token,))
        return rows[0] if rows else None

    def list_rollbacks(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM rollback_records ORDER BY created_at")

    def mark_rollback_restored(self, token: str) -> None:
        self._exec(
            "UPDATE rollback_records SET restored_at=?, status=? WHERE token=?",
            (time.time(), "restored", token),
        )

    def add_rollback(self, r: dict[str, Any]) -> None:
        self._exec(
            """INSERT OR REPLACE INTO rollback_records(token,run_id,tool_call_id,kind,target,
                 snapshot_path,manifest_json,created_at,restored_at,status)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                r["token"], r.get("run_id"), r.get("tool_call_id"), r.get("kind"),
                r.get("target"), r.get("snapshot_path"), _j(r.get("manifest", [])),
                r.get("created_at", time.time()), r.get("restored_at"), r.get("status", "active"),
            ),
        )

    # -- settings ---------------------------------------------------------- #
    def set_setting(self, key: str, value: Any, source: str = "runtime") -> None:
        self._exec(
            "INSERT OR REPLACE INTO settings(key,value_json,source,updated_at) VALUES (?,?,?,?)",
            (key, _j(value), source, time.time()),
        )

    def get_setting(self, key: str) -> Any | None:
        rows = self._query("SELECT value_json FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value_json"]) if rows else None

    # -- vault files (index bookkeeping) ----------------------------------- #
    def upsert_vault_file(self, path: str, sha256: str, mtime: float, type_: str, link_count: int) -> None:
        self._exec(
            "INSERT OR REPLACE INTO vault_files(path,sha256,mtime,type,indexed_at,link_count) VALUES (?,?,?,?,?,?)",
            (path, sha256, mtime, type_, time.time(), link_count),
        )

    def list_vault_files(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM vault_files ORDER BY path")

    # -- memory note catalog (ported from gamma; Markdown stays canonical) -- #
    def upsert_memory_note(self, note: dict[str, Any]) -> None:
        self._exec(
            """INSERT INTO memory_notes(vault_path,id,type,title,project_id,source,source_uri,
                 created_at,updated_at,content_hash,frontmatter_json,summary,search_text,deleted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(vault_path) DO UPDATE SET
                 type=excluded.type, title=excluded.title, project_id=excluded.project_id,
                 source=excluded.source, source_uri=excluded.source_uri, updated_at=excluded.updated_at,
                 content_hash=excluded.content_hash, frontmatter_json=excluded.frontmatter_json,
                 summary=excluded.summary, search_text=excluded.search_text, deleted_at=NULL""",
            (
                note["vault_path"], note.get("id"), note.get("type", "note"), note.get("title"),
                note.get("project_id"), note.get("source"), note.get("source_uri"),
                note.get("created_at", time.time()), note.get("updated_at", time.time()),
                note.get("content_hash"), _j(note.get("frontmatter", {})),
                note.get("summary"), note.get("search_text"),
            ),
        )

    def get_memory_note(self, vault_path: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM memory_notes WHERE vault_path=? AND deleted_at IS NULL", (vault_path,))
        return rows[0] if rows else None

    # -- Memory v2: tier / lifecycle -------------------------------------- #
    def set_memory_tier(self, vault_path: str, tier: str, *, salience: float | None = None,
                        group_id: str | None = None, superseded_by: str | None = None) -> None:
        """Move a note between tiers (short|medium|long|archive). Never deletes — 'archive' is the
        terminal, recall-excluded tier, and superseded_by records merge/replacement lineage."""
        sets, params = ["tier=?"], [tier]
        if salience is not None:
            sets.append("salience=?"); params.append(salience)
        if group_id is not None:
            sets.append("group_id=?"); params.append(group_id)
        if superseded_by is not None:
            sets.append("superseded_by=?"); params.append(superseded_by)
        params.append(vault_path)
        self._exec(f"UPDATE memory_notes SET {', '.join(sets)} WHERE vault_path=?", tuple(params))

    def touch_memory_note(self, vault_path: str) -> None:
        """Record a recall hit: bump access_count + last_accessed (feeds recurrence promotion)."""
        self._exec("UPDATE memory_notes SET access_count=COALESCE(access_count,0)+1, last_accessed=? "
                   "WHERE vault_path=?", (time.time(), vault_path))

    def set_memory_meta(self, vault_path: str, *, confidence: float | None = None,
                        source_run_id: str | None = None) -> None:
        sets, params = [], []
        if confidence is not None:
            sets.append("confidence=?"); params.append(confidence)
        if source_run_id is not None:
            sets.append("source_run_id=?"); params.append(source_run_id)
        if not sets:
            return
        params.append(vault_path)
        self._exec(f"UPDATE memory_notes SET {', '.join(sets)} WHERE vault_path=?", tuple(params))

    def archive_operational_logs(self) -> int:
        """One-time cleanup: mark stale auto-generated run/garden logs under daily/ as archived so
        they are excluded from recall. Files stay on disk (never delete). Returns count archived."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory_notes SET tier='archive' "
                "WHERE (vault_path LIKE 'daily/run-%' OR vault_path LIKE 'daily/garden-%') "
                "AND (tier IS NULL OR tier!='archive')")
            self._conn.commit()
            return cur.rowcount

    # -- Memory v2: embedding vectors ------------------------------------- #
    def set_memory_vector(self, vault_path: str, embedding: list[float], model: str, dim: int) -> None:
        import array
        blob = array.array("f", embedding).tobytes()
        self._exec("INSERT INTO memory_vectors(vault_path,embedding,model,dim,updated_at) "
                   "VALUES (?,?,?,?,?) ON CONFLICT(vault_path) DO UPDATE SET "
                   "embedding=excluded.embedding, model=excluded.model, dim=excluded.dim, "
                   "updated_at=excluded.updated_at",
                   (vault_path, blob, model, dim, time.time()))

    @staticmethod
    def _unpack_vec(blob: bytes) -> list[float]:
        import array
        a = array.array("f"); a.frombytes(blob)
        return list(a)

    def get_memory_vector(self, vault_path: str) -> list[float] | None:
        rows = self._query("SELECT embedding FROM memory_vectors WHERE vault_path=?", (vault_path,))
        return self._unpack_vec(rows[0]["embedding"]) if rows else None

    def all_memory_vectors(self, exclude_archived: bool = True) -> list[dict[str, Any]]:
        """All (vault_path, embedding) for brute-force cosine search. Skips archived/daily notes
        and any whose note row is deleted."""
        rows = self._query(
            "SELECT v.vault_path AS vault_path, v.embedding AS embedding FROM memory_vectors v "
            "JOIN memory_notes n ON n.vault_path=v.vault_path "
            "WHERE n.deleted_at IS NULL "
            + ("AND n.tier!='archive' AND n.vault_path NOT LIKE 'daily/%'" if exclude_archived else ""))
        return [{"vault_path": r["vault_path"], "embedding": self._unpack_vec(r["embedding"])} for r in rows]

    def paths_without_vectors(self) -> list[str]:
        """Notes in the catalog that have no stored embedding yet (for backfill)."""
        rows = self._query(
            "SELECT n.vault_path AS vault_path FROM memory_notes n "
            "LEFT JOIN memory_vectors v ON v.vault_path=n.vault_path "
            "WHERE n.deleted_at IS NULL AND v.vault_path IS NULL")
        return [r["vault_path"] for r in rows]

    # -- Memory v2: groups (entity / topic concept notes) ----------------- #
    def upsert_memory_group(self, group: dict[str, Any]) -> None:
        import array
        centroid = group.get("centroid")
        blob = array.array("f", centroid).tobytes() if centroid else None
        now = time.time()
        self._exec(
            "INSERT INTO memory_groups(id,kind,title,description,tier,centroid,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "kind=excluded.kind, title=excluded.title, description=excluded.description, "
            "tier=excluded.tier, centroid=excluded.centroid, updated_at=excluded.updated_at",
            (group["id"], group.get("kind", "topic"), group.get("title"), group.get("description"),
             group.get("tier", "long"), blob, now, now))

    def list_memory_groups(self) -> list[dict[str, Any]]:
        return self._query("SELECT id,kind,title,description,tier,created_at,updated_at "
                           "FROM memory_groups ORDER BY updated_at DESC")

    def group_members(self, group_id: str) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM memory_notes WHERE group_id=? AND deleted_at IS NULL "
                           "ORDER BY updated_at DESC", (group_id,))

    def clear_memory_groups(self) -> None:
        """Drop all groups and un-stamp members. Groups are derived (rebuilt by the librarian), so
        this is safe — used to reset after a bad grouping pass."""
        with self._lock:
            self._conn.execute("DELETE FROM memory_groups")
            self._conn.execute("UPDATE memory_notes SET group_id=NULL WHERE group_id IS NOT NULL")
            self._conn.commit()

    def search_memory_notes(self, query: str, project_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        like = f"%{query}%"
        if project_id:
            return self._query(
                """SELECT * FROM memory_notes WHERE deleted_at IS NULL AND project_id=?
                   AND (title LIKE ? OR summary LIKE ? OR search_text LIKE ? OR vault_path LIKE ?)
                   ORDER BY updated_at DESC LIMIT ?""",
                (project_id, like, like, like, like, limit),
            )
        return self._query(
            """SELECT * FROM memory_notes WHERE deleted_at IS NULL
               AND (title LIKE ? OR summary LIKE ? OR search_text LIKE ? OR vault_path LIKE ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (like, like, like, like, limit),
        )

    def list_memory_notes(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM memory_notes WHERE deleted_at IS NULL ORDER BY vault_path")

    def mark_memory_note_deleted(self, vault_path: str) -> None:
        self._exec("UPDATE memory_notes SET deleted_at=? WHERE vault_path=?", (time.time(), vault_path))
        self._exec("DELETE FROM memory_links WHERE from_path=?", (vault_path,))

    def replace_links(self, from_path: str, link_texts: list[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memory_links WHERE from_path=?", (from_path,))
            now = time.time()
            for lt in dict.fromkeys(link_texts):  # de-dup, preserve order
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory_links(from_path,link_text,created_at) VALUES (?,?,?)",
                    (from_path, lt, now),
                )
            self._conn.commit()

    def links_from(self, from_path: str) -> list[str]:
        return [r["link_text"] for r in self._query("SELECT link_text FROM memory_links WHERE from_path=?", (from_path,))]

    def all_links(self) -> list[dict[str, Any]]:
        return self._query("SELECT from_path, link_text FROM memory_links")
