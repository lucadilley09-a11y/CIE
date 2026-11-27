"""SQLite + FTS5 storage manager, single-writer lock, and content-hash result cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .config import get_cie_home, get_vault_root, utc_now


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files_index (
    path TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    language TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT DEFAULT '',
    parent TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols_index(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols_index(name);

CREATE TABLE IF NOT EXISTS dependencies_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence TEXT NOT NULL,
    project_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deps_source ON dependencies_index(source_file);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    file_path,
    project_id UNINDEXED,
    content
);

CREATE TABLE IF NOT EXISTS result_cache (
    cache_key TEXT PRIMARY KEY,
    op_name TEXT NOT NULL,
    op_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes_index (
    rel_path TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    title TEXT NOT NULL,
    note_type TEXT NOT NULL,
    project TEXT DEFAULT '',
    status TEXT DEFAULT 'observed',
    confidence TEXT DEFAULT 'observed',
    tags TEXT DEFAULT '',
    updated_at TEXT NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes_links (
    source_path TEXT NOT NULL,
    target_title TEXT NOT NULL,
    target_path TEXT DEFAULT '',
    is_resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notes_links_target ON notes_links(target_title);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    rel_path,
    title,
    tags,
    body
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    message TEXT DEFAULT '',
    result_json TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    description TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_id TEXT NOT NULL,
    elapsed_ms REAL NOT NULL,
    input_bytes INTEGER NOT NULL DEFAULT 0,
    output_bytes INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deps_project ON dependencies_index(project_id);

-- Per-project index schema version. When CIE's indexer changes what it
-- extracts, bumping INDEX_VERSION forces one full re-index (safe migration).
CREATE TABLE IF NOT EXISTS index_meta (
    project_id TEXT PRIMARY KEY,
    index_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class StorageManager:
    """Thread-safe SQLite + FTS5 manager with single-writer serialization."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            vault_cie = get_vault_root() / ".cie"
            vault_cie.mkdir(parents=True, exist_ok=True)
            db_path = vault_cie / "index.sqlite"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def run_write(self, fn: Any) -> Any:
        with self._write_lock:
            conn = self._connect()
            try:
                res = fn(conn)
                conn.commit()
                return res
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # --- Content-hash result cache ---
    def make_cache_key(self, op_name: str, op_version: str, payload: Any) -> tuple[str, str]:
        encoded = json.dumps(payload, sort_keys=True, default=str)
        input_hash = sha256_text(encoded)
        return f"{op_name}:{op_version}:{input_hash}", input_hash

    def cache_get(self, cache_key: str) -> dict[str, Any] | None:
        rows = self.query("SELECT result_json FROM result_cache WHERE cache_key = ?", (cache_key,))
        if not rows:
            return None
        self.execute(
            "UPDATE result_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
            (cache_key,),
        )
        try:
            return json.loads(rows[0]["result_json"])
        except Exception:
            return None

    def cache_set(self, cache_key: str, op_name: str, op_version: str, input_hash: str, result: Any) -> None:
        raw = json.dumps(result, default=str)
        self.execute(
            """
            INSERT INTO result_cache(cache_key, op_name, op_version, input_hash, result_json, created_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(cache_key) DO UPDATE SET
                result_json = excluded.result_json,
                created_at = excluded.created_at
            """,
            (cache_key, op_name, op_version, input_hash, raw, utc_now()),
        )

    def cache_stats(self) -> dict[str, Any]:
        rows = self.query(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hit_count), 0) AS total_hits, COALESCE(SUM(LENGTH(result_json)), 0) AS bytes FROM result_cache"
        )
        by_op = self.query(
            "SELECT op_name, COUNT(*) AS entries, COALESCE(SUM(hit_count), 0) AS hits FROM result_cache GROUP BY op_name ORDER BY hits DESC"
        )
        return {
            "summary": rows[0] if rows else {"entries": 0, "total_hits": 0, "bytes": 0},
            "by_operation": by_op,
        }

    def cache_clear(self, op_name: str | None = None) -> int:
        if op_name:
            return self.execute("DELETE FROM result_cache WHERE op_name = ?", (op_name,))
        return self.execute("DELETE FROM result_cache")

    def record_benchmark(
        self,
        capability_id: str,
        elapsed_ms: float,
        input_bytes: int = 0,
        output_bytes: int = 0,
        cache_hit: bool = False,
    ) -> None:
        self.execute(
            """
            INSERT INTO benchmarks(capability_id, elapsed_ms, input_bytes, output_bytes, cache_hit, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (capability_id, round(elapsed_ms, 3), input_bytes, output_bytes, 1 if cache_hit else 0, utc_now()),
        )

    def index_stats(self) -> dict[str, Any]:
        files = self.query("SELECT COUNT(*) AS c FROM files_index")[0]["c"]
        symbols = self.query("SELECT COUNT(*) AS c FROM symbols_index")[0]["c"]
        deps = self.query("SELECT COUNT(*) AS c FROM dependencies_index")[0]["c"]
        notes = self.query("SELECT COUNT(*) AS c FROM notes_index")[0]["c"]
        per_project = self.query(
            """
            SELECT f.project_id AS project_id,
                   COUNT(*) AS files,
                   (SELECT COUNT(*) FROM symbols_index s WHERE s.project_id = f.project_id) AS symbols,
                   (SELECT COUNT(*) FROM dependencies_index d WHERE d.project_id = f.project_id) AS dependencies,
                   (SELECT MAX(indexed_at) FROM files_index g WHERE g.project_id = f.project_id) AS last_indexed_at
            FROM files_index f GROUP BY f.project_id ORDER BY f.project_id
            """
        )
        return {
            "db_path": str(self.db_path),
            "files_indexed": files,
            "symbols_indexed": symbols,
            "dependencies_indexed": deps,
            "notes_indexed": notes,
            "projects": per_project,
        }


_DEFAULT_STORAGE: StorageManager | None = None


def get_storage() -> StorageManager:
    global _DEFAULT_STORAGE
    db_path = get_vault_root() / ".cie" / "index.sqlite"
    if _DEFAULT_STORAGE is None or _DEFAULT_STORAGE.db_path != db_path:
        _DEFAULT_STORAGE = StorageManager(db_path)
    return _DEFAULT_STORAGE