"""Phase 1.3 storage: content-addressed blob store, snapshots, preview-first
rollback, retention, usage reporting, and duplicate detection.

Guarantees:
- Each unique file content is stored once (SHA-256), so unchanged files cost
  nothing in later snapshots.
- Compression is lossless (zlib) and only kept when it actually saves space.
- Every blob is re-hashed when read; a corrupt blob is never written back.
- A restore always takes a safety snapshot of everything it will touch first,
  so a rollback can itself be rolled back.
- Retention never removes named or pinned snapshots, and pruning is a
  separate, approval-gated step with a dry run.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import os
import shutil
import tempfile
import uuid
import zlib
from pathlib import Path
from typing import Any, Iterable

from .config import (
    DEFAULT_EXCLUDES,
    MODEL_WEIGHT_EXTS,
    get_cie_home,
    get_vault_root,
    iter_project_files,
    load_config,
    project_roots,
    resolve_in_projects,
    utc_now,
)
from .storage import get_storage

CHUNK = 1024 * 1024
# Formats that are already compressed; zlib would waste time for ~0 gain.
PRECOMPRESSED_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ogg", ".mp3", ".m4a", ".aac", ".flac",
    ".mp4", ".webm", ".mkv", ".mov", ".zip", ".7z", ".rar", ".gz", ".xz", ".bz2",
    ".zst", ".whl", ".jar", ".mcpb", ".glb", ".ktx2", ".basis", ".woff2",
}
MIN_SAVINGS = 0.05  # keep compressed form only if it saves at least 5%
TEXT_DIFF_EXTS = {
    ".py", ".lua", ".luau", ".js", ".ts", ".jsx", ".tsx", ".json", ".md", ".txt", ".yaml",
    ".yml", ".toml", ".html", ".css", ".gd", ".cs", ".cpp", ".c", ".h", ".java", ".kt",
    ".rs", ".go", ".cfg", ".ini", ".xml",
}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def storage_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {"auto_snapshot": True, "retention_days": 30, "limit_gb": 5.0}
    raw = load_config().get("storage")
    if isinstance(raw, dict):
        if isinstance(raw.get("auto_snapshot"), bool):
            settings["auto_snapshot"] = raw["auto_snapshot"]
        if isinstance(raw.get("retention_days"), int) and not isinstance(raw["retention_days"], bool) and raw["retention_days"] >= 1:
            settings["retention_days"] = raw["retention_days"]
        if isinstance(raw.get("limit_gb"), (int, float)) and not isinstance(raw["limit_gb"], bool) and raw["limit_gb"] > 0:
            settings["limit_gb"] = float(raw["limit_gb"])
    return settings


def store_root() -> Path:
    root = get_cie_home() / "store"
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------
# Blob store
# --------------------------------------------------------------------------

def _blob_path(sha: str) -> Path:
    return store_root() / "blobs" / sha[:2] / sha


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def put_file(path: Path) -> dict[str, Any]:
    """Store a file's content (once). Returns {sha256, size, stored_size, codec, new}."""
    storage = get_storage()
    sha, size = _hash_file(path)
    existing = storage.query("SELECT size, stored_size, codec FROM blobs WHERE sha256 = ?", (sha,))
    target = _blob_path(sha)
    if existing and target.exists():
        row = existing[0]
        return {"sha256": sha, "size": row["size"], "stored_size": row["stored_size"], "codec": row["codec"], "new": False}

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".blob-", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        check = hashlib.sha256()
        codec = "raw"
        with os.fdopen(fd, "wb") as out, path.open("rb") as src:
            if path.suffix.lower() in PRECOMPRESSED_EXTS:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    check.update(chunk)
                    out.write(chunk)
            else:
                codec = "zlib"
                comp = zlib.compressobj(6)
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    check.update(chunk)
                    out.write(comp.compress(chunk))
                out.write(comp.flush())
        if check.hexdigest() != sha:
            raise RuntimeError(f"{path} changed while it was being stored; try again.")
        stored = tmp.stat().st_size
        if codec == "zlib" and size > 0 and stored > size * (1 - MIN_SAVINGS):
            # Not worth it: store raw instead.
            shutil.copyfile(path, tmp)
            codec = "raw"
            stored = tmp.stat().st_size
            if _hash_file(tmp)[0] != sha:
                raise RuntimeError(f"{path} changed while it was being stored; try again.")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    storage.execute(
        "INSERT OR REPLACE INTO blobs(sha256, size, stored_size, codec, created_at) VALUES (?, ?, ?, ?, ?)",
        (sha, size, stored, codec, utc_now()),
    )
    return {"sha256": sha, "size": size, "stored_size": stored, "codec": codec, "new": True}


def read_blob_to(sha: str, dest: Path) -> None:
    """Write a verified copy of a blob to ``dest`` atomically."""
    rows = get_storage().query("SELECT codec FROM blobs WHERE sha256 = ?", (sha,))
    src = _blob_path(sha)
    if not rows or not src.exists():
        raise FileNotFoundError(f"Stored content {sha[:12]} is missing from the CIE store.")
    codec = rows[0]["codec"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".restore-", dir=str(dest.parent))
    tmp = Path(tmp_name)
    try:
        check = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, src.open("rb") as fh:
            dec = zlib.decompressobj() if codec == "zlib" else None
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                data = dec.decompress(chunk) if dec else chunk
                check.update(data)
                out.write(data)
            if dec:
                tail = dec.flush()
                check.update(tail)
                out.write(tail)
        if check.hexdigest() != sha:
            raise RuntimeError(f"Stored content {sha[:12]} failed its integrity check; not restored.")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_blob_bytes(sha: str, limit: int = 2_000_000) -> bytes | None:
    rows = get_storage().query("SELECT codec, size FROM blobs WHERE sha256 = ?", (sha,))
    if not rows or rows[0]["size"] > limit:
        return None
    raw = _blob_path(sha).read_bytes()
    return zlib.decompress(raw) if rows[0]["codec"] == "zlib" else raw


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------

def _expand(paths: Iterable[str]) -> tuple[list[Path], str]:
    """Resolve requested paths inside registered projects. Directories expand
    to their files. Missing files are kept (recorded as 'did not exist')."""
    out: list[Path] = []
    project_id = ""
    for raw in paths:
        resolved, root = resolve_in_projects(str(raw))
        project_id = project_id or root["id"]
        if resolved.is_dir():
            excludes = set(DEFAULT_EXCLUDES) | set(root.get("excludes") or [])
            for dirpath, dirnames, filenames in os.walk(resolved, followlinks=False):
                dirnames[:] = [d for d in dirnames if d not in excludes]
                out.extend(Path(dirpath) / f for f in filenames if not (Path(dirpath) / f).is_symlink())
        else:
            out.append(resolved)
    seen: set[str] = set()
    unique = []
    for p in out:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique, project_id


def _prior_hash(path: str, size: int, mtime_ns: int) -> str | None:
    """Reuse the hash from an earlier snapshot when size and mtime match,
    so repeat snapshots of large projects only read changed files."""
    rows = get_storage().query(
        "SELECT sha256 FROM snapshot_files WHERE path = ? AND size = ? AND mtime_ns = ? AND existed = 1 "
        "AND sha256 IS NOT NULL LIMIT 1",
        (path, size, mtime_ns),
    )
    if rows and _blob_path(rows[0]["sha256"]).exists():
        return rows[0]["sha256"]
    return None


def create_snapshot(
    paths: list[str] | None = None,
    project_id: str = "",
    name: str = "",
    description: str = "",
    kind: str = "manual",
) -> dict[str, Any]:
    if not paths and not project_id:
        raise ValueError("Give 'paths' or a 'project_id' to snapshot.")
    scope = "paths"
    if paths:
        files, pid = _expand(paths)
    else:
        roots = [r for r in project_roots() if r["id"] == project_id]
        if not roots:
            raise ValueError(f"No registered project with id '{project_id}'.")
        files = [f for f in iter_project_files(roots[0], include_binary=True)
                 if f.suffix.lower() not in MODEL_WEIGHT_EXTS]
        pid = project_id
        scope = "project"
    snap_id = f"snap_{uuid.uuid4().hex[:12]}"
    entries: list[tuple[str, str | None, int, int, int]] = []
    new_blobs = 0
    new_stored = 0
    logical = 0
    errors: list[str] = []
    for f in files:
        sp = str(f)
        if not f.exists():
            entries.append((sp, None, 0, 0, 0))
            continue
        try:
            st = f.stat()
            sha = _prior_hash(sp, st.st_size, st.st_mtime_ns)
            if sha is None:
                info = put_file(f)
                sha = info["sha256"]
                if info["new"]:
                    new_blobs += 1
                    new_stored += info["stored_size"]
            entries.append((sp, sha, st.st_size, st.st_mtime_ns, 1))
            logical += st.st_size
        except (OSError, RuntimeError) as exc:
            errors.append(f"{sp}: {exc}")

    storage = get_storage()

    def _tx(conn: Any) -> None:
        conn.execute(
            "INSERT INTO snapshots(snapshot_id, project_id, description, manifest_json, created_at, name, kind, pinned, file_count, logical_bytes, scope) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)",
            (snap_id, pid or "default", description or name or f"{kind} snapshot", utc_now(), name, kind,
             1 if name else 0, sum(1 for e in entries if e[4]), logical, scope),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO snapshot_files(snapshot_id, path, sha256, size, mtime_ns, existed) VALUES (?, ?, ?, ?, ?, ?)",
            [(snap_id, *e) for e in entries],
        )

    storage.run_write(_tx)
    result: dict[str, Any] = {
        "snapshot_id": snap_id,
        "project_id": pid,
        "kind": kind,
        "name": name,
        "files_saved": sum(1 for e in entries if e[4]),
        "files_absent": sum(1 for e in entries if not e[4]),
        "logical_bytes": logical,
        "new_blobs": new_blobs,
        "new_stored_bytes": new_stored,
    }
    if errors:
        result["errors"] = errors[:20]
        result["error_count"] = len(errors)
    usage = usage_summary()
    if usage["over_limit"]:
        result["warning"] = (
            f"CIE storage is {usage['total_gb']} GB, over the {usage['limit_gb']} GB limit. "
            "Run storage.prune with dry_run to see what can be cleaned."
        )
    return result


def _snapshot_row(snapshot_id: str) -> dict[str, Any]:
    rows = get_storage().query("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
    if not rows:
        raise ValueError(f"Snapshot '{snapshot_id}' not found.")
    return rows[0]


def _entries(snapshot_id: str) -> list[dict[str, Any]]:
    return get_storage().query(
        "SELECT path, sha256, size, existed FROM snapshot_files WHERE snapshot_id = ? ORDER BY path", (snapshot_id,)
    )


def _current_hash(path: Path, expected_size: int | None = None) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return _hash_file(path)[0]


def preview_restore(snapshot_id: str, remove_added: bool = False) -> dict[str, Any]:
    row = _snapshot_row(snapshot_id)
    entries = _entries(snapshot_id)
    if not entries and row.get("manifest_json") not in (None, "", "[]"):
        return {"snapshot_id": snapshot_id, "legacy": True, "note": "Legacy snapshot: restores its saved files directly."}
    will_restore, will_recreate, will_delete, unchanged = [], [], [], 0
    for e in entries:
        path = Path(e["path"])
        current = _current_hash(path)
        if e["existed"]:
            if current is None:
                will_recreate.append(e["path"])
            elif current != e["sha256"]:
                will_restore.append(e["path"])
            else:
                unchanged += 1
        elif current is not None:
            will_delete.append(e["path"])
        else:
            unchanged += 1
    added_since: list[str] = []
    if row.get("scope") == "project":
        roots = [r for r in project_roots() if r["id"] == row["project_id"]]
        if roots:
            known = {os.path.normcase(e["path"]) for e in entries}
            added_since = [str(p) for p in iter_project_files(roots[0], include_binary=True)
                           if os.path.normcase(str(p)) not in known]
    if remove_added:
        will_delete.extend(added_since)
    return {
        "snapshot_id": snapshot_id,
        "name": row.get("name") or "",
        "description": row.get("description"),
        "created_at": row.get("created_at"),
        "will_overwrite": will_restore,
        "will_recreate": will_recreate,
        "will_delete": will_delete,
        "unchanged": unchanged,
        "added_since_snapshot": [] if remove_added else added_since,
        "note": "Files added since the snapshot are kept unless remove_added is true." if added_since and not remove_added else "",
        "change_count": len(will_restore) + len(will_recreate) + len(will_delete),
    }


def restore_snapshot(snapshot_id: str, remove_added: bool = False) -> dict[str, Any]:
    plan = preview_restore(snapshot_id, remove_added)
    if plan.get("legacy"):
        return _restore_legacy(snapshot_id)
    touched = plan["will_overwrite"] + plan["will_recreate"] + plan["will_delete"]
    if not touched:
        return {**plan, "restored": 0, "deleted": 0, "safety_snapshot_id": None, "note": "Nothing to change."}
    safety = create_snapshot(
        paths=touched, kind="safety",
        description=f"Automatic safety snapshot before restoring {snapshot_id}",
    )
    by_path = {e["path"]: e for e in _entries(snapshot_id)}
    restored, deleted, errors = 0, 0, []
    for p in plan["will_overwrite"] + plan["will_recreate"]:
        try:
            resolved, _ = resolve_in_projects(p)
            read_blob_to(by_path[p]["sha256"], resolved)
            restored += 1
        except Exception as exc:  # noqa: BLE001 - report per file, keep going
            errors.append(f"{p}: {exc}")
    for p in plan["will_delete"]:
        try:
            resolved, _ = resolve_in_projects(p)
            if resolved.is_file():
                resolved.unlink()
                deleted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{p}: {exc}")
    out = {
        "snapshot_id": snapshot_id,
        "restored": restored,
        "deleted": deleted,
        "safety_snapshot_id": safety["snapshot_id"],
        "undo": f"Restore {safety['snapshot_id']} to undo this rollback.",
    }
    if errors:
        out["errors"] = errors
    return out


def _restore_legacy(snapshot_id: str) -> dict[str, Any]:
    import json

    row = _snapshot_row(snapshot_id)
    manifest = json.loads(row["manifest_json"] or "[]")
    targets = [item["original_path"] for item in manifest]
    safety = create_snapshot(paths=targets, kind="safety", description=f"Safety snapshot before restoring legacy {snapshot_id}") if targets else None
    restored = []
    for item in manifest:
        orig, _ = resolve_in_projects(item["original_path"])
        blob = Path(item["blob_path"])
        if blob.exists() and hashlib.sha256(blob.read_bytes()).hexdigest() == item.get("sha256", ""):
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(blob, orig)
            restored.append(str(orig))
    return {
        "snapshot_id": snapshot_id,
        "restored_files": restored,
        "count": len(restored),
        "safety_snapshot_id": safety["snapshot_id"] if safety else None,
    }


def list_snapshots(project_id: str = "", limit: int = 20, include_auto: bool = True) -> dict[str, Any]:
    sql = "SELECT snapshot_id, project_id, name, kind, pinned, description, file_count, logical_bytes, scope, created_at FROM snapshots"
    clauses, params = [], []
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if not include_auto:
        clauses.append("kind != 'auto'")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    rows = get_storage().query(sql, tuple(params))
    return {"snapshots": rows, "count": len(rows)}


def name_snapshot(snapshot_id: str, name: str, pinned: bool = True) -> dict[str, Any]:
    _snapshot_row(snapshot_id)
    get_storage().execute(
        "UPDATE snapshots SET name = ?, pinned = ? WHERE snapshot_id = ?",
        (name.strip(), 1 if pinned or name.strip() else 0, snapshot_id),
    )
    return {"snapshot_id": snapshot_id, "name": name.strip(), "pinned": bool(pinned or name.strip()),
            "note": "Named and pinned snapshots are never removed by retention."}


def compare(snapshot_a: str, snapshot_b: str = "", max_diff_lines: int = 60) -> dict[str, Any]:
    """Compare two snapshots, or a snapshot against the current files."""
    a = {e["path"]: e for e in _entries(snapshot_a) if e["existed"]}
    if snapshot_b:
        b = {e["path"]: e for e in _entries(snapshot_b) if e["existed"]}
        label_b = snapshot_b
    else:
        b = {}
        for p in a:
            h = _current_hash(Path(p))
            if h:
                b[p] = {"path": p, "sha256": h}
        label_b = "current files"
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    modified = sorted(p for p in set(a) & set(b) if a[p]["sha256"] != b[p]["sha256"])
    diffs = []
    budget = max(0, int(max_diff_lines))
    for p in modified:
        if budget <= 0 or Path(p).suffix.lower() not in TEXT_DIFF_EXTS:
            continue
        old = read_blob_bytes(a[p]["sha256"])
        new = read_blob_bytes(b[p]["sha256"]) if snapshot_b else (Path(p).read_bytes() if Path(p).stat().st_size <= 2_000_000 else None)
        if old is None or new is None:
            continue
        lines = list(difflib.unified_diff(
            old.decode("utf-8", "replace").splitlines(), new.decode("utf-8", "replace").splitlines(),
            fromfile=f"{snapshot_a}:{Path(p).name}", tofile=f"{label_b}:{Path(p).name}", lineterm="", n=2,
        ))
        diffs.append({"path": p, "diff": lines[:budget], "truncated": len(lines) > budget})
        budget -= len(lines[:budget])
    return {
        "from": snapshot_a, "to": label_b,
        "added": added, "removed": removed, "modified": modified,
        "text_diffs": diffs,
    }


# --------------------------------------------------------------------------
# Retention and usage
# --------------------------------------------------------------------------

def _dir_bytes(path: Path) -> int:
    total = 0
    if path.exists():
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += (Path(dirpath) / f).stat().st_size
                except OSError:
                    pass
    return total


def usage_summary() -> dict[str, Any]:
    home = get_cie_home()
    settings = storage_settings()
    total = _dir_bytes(home)
    limit_gb = float(settings["limit_gb"])
    return {
        "total_bytes": total,
        "total_gb": round(total / 1024**3, 3),
        "limit_gb": limit_gb,
        "over_limit": total > limit_gb * 1024**3,
    }


def prune(dry_run: bool = True) -> dict[str, Any]:
    """Remove unnamed, unpinned snapshots older than the retention window,
    then delete blobs no snapshot references. Named/pinned are always kept."""
    storage = get_storage()
    days = int(storage_settings()["retention_days"])
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).replace(microsecond=0).isoformat()
    old = storage.query(
        "SELECT snapshot_id, kind, created_at, logical_bytes FROM snapshots "
        "WHERE pinned = 0 AND name = '' AND created_at < ? ORDER BY created_at",
        (cutoff,),
    )
    old_ids = [r["snapshot_id"] for r in old]
    keep_refs = {
        r["sha256"] for r in storage.query(
            "SELECT DISTINCT sha256 FROM snapshot_files WHERE sha256 IS NOT NULL"
            + (f" AND snapshot_id NOT IN ({','.join('?' for _ in old_ids)})" if old_ids else ""),
            tuple(old_ids),
        )
    }
    orphan = storage.query("SELECT sha256, stored_size FROM blobs")
    orphan = [b for b in orphan if b["sha256"] not in keep_refs]
    freed = sum(b["stored_size"] for b in orphan)
    report = {
        "dry_run": dry_run,
        "retention_days": days,
        "snapshots_to_remove": len(old_ids),
        "blobs_to_remove": len(orphan),
        "bytes_to_free": freed,
        "kept": "Named and pinned snapshots, and everything newer than the retention window.",
    }
    if dry_run:
        report["oldest_candidates"] = old[:10]
        return report

    def _tx(conn: Any) -> None:
        for sid in old_ids:
            conn.execute("DELETE FROM snapshot_files WHERE snapshot_id = ?", (sid,))
            conn.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (sid,))
        for b in orphan:
            conn.execute("DELETE FROM blobs WHERE sha256 = ?", (b["sha256"],))

    storage.run_write(_tx)
    for b in orphan:
        _blob_path(b["sha256"]).unlink(missing_ok=True)
    for sid in old_ids:  # legacy per-snapshot folders
        shutil.rmtree(get_vault_root() / ".cie" / "snapshots" / sid, ignore_errors=True)
    report["removed"] = True
    return report


def storage_report() -> dict[str, Any]:
    storage = get_storage()
    home = get_cie_home()
    blobs = storage.query("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS logical, COALESCE(SUM(stored_size),0) AS stored FROM blobs")[0]
    snaps = storage.query(
        "SELECT kind, COUNT(*) AS n, COALESCE(SUM(logical_bytes),0) AS logical FROM snapshots GROUP BY kind"
    )
    referenced = storage.query("SELECT COALESCE(SUM(size),0) AS b FROM snapshot_files WHERE existed = 1")[0]["b"]
    db_bytes = _dir_bytes(storage.db_path.parent)
    cache = storage.cache_stats()["summary"]
    usage = usage_summary()
    savings = referenced - blobs["stored"]
    out = {
        **usage,
        "store": {
            "unique_blobs": blobs["n"],
            "unique_bytes": blobs["logical"],
            "stored_bytes": blobs["stored"],
            "bytes_referenced_by_snapshots": referenced,
            "saved_by_dedupe_and_compression": savings,
            "saved_percent": round(100 * savings / referenced, 1) if referenced else 0.0,
        },
        "snapshots_by_kind": {r["kind"]: {"count": r["n"], "logical_bytes": r["logical"]} for r in snaps},
        "index_and_vault_bytes": db_bytes,
        "result_cache": cache,
        "models_bytes": _dir_bytes(home / "models"),
        "archives_bytes": _dir_bytes(home / "archives"),
    }
    if usage["over_limit"]:
        p = prune(dry_run=True)
        out["suggestions"] = [
            f"storage.prune could free {p['bytes_to_free']} bytes ({p['snapshots_to_remove']} old snapshots).",
            "eff.cache_inspector shows the result cache, which is safe to clear and rebuilds itself.",
        ]
    return out


# --------------------------------------------------------------------------
# Duplicate report (read-only)
# --------------------------------------------------------------------------

def duplicate_report(project_id: str, top: int = 20) -> dict[str, Any]:
    roots = [r for r in project_roots() if r["id"] == project_id]
    if not roots:
        raise ValueError(f"No registered project with id '{project_id}'.")
    files = iter_project_files(roots[0], include_binary=True)
    by_size: dict[int, list[Path]] = {}
    sizes: list[tuple[int, str]] = []
    for f in files:
        try:
            s = f.stat().st_size
        except OSError:
            continue
        sizes.append((s, str(f)))
        if s > 0:
            by_size.setdefault(s, []).append(f)
    groups = []
    for size, same in by_size.items():
        if len(same) < 2:
            continue
        by_hash: dict[str, list[str]] = {}
        for f in same:
            try:
                by_hash.setdefault(_hash_file(f)[0], []).append(str(f))
            except OSError:
                continue
        for sha, paths in by_hash.items():
            if len(paths) > 1:
                groups.append({"sha256": sha, "size": size, "copies": len(paths),
                               "wasted_bytes": size * (len(paths) - 1), "paths": paths})
    groups.sort(key=lambda g: g["wasted_bytes"], reverse=True)
    sizes.sort(reverse=True)
    return {
        "project_id": project_id,
        "files_scanned": len(sizes),
        "total_bytes": sum(s for s, _ in sizes),
        "duplicate_groups": len(groups),
        "wasted_bytes": sum(g["wasted_bytes"] for g in groups),
        "top_duplicates": groups[:top],
        "largest_files": [{"path": p, "size": s} for s, p in sizes[:top]],
        "note": "Report only. Nothing was changed.",
    }


# --------------------------------------------------------------------------
# Lossless PNG optimization
# --------------------------------------------------------------------------

def optimize_png(paths: list[str], apply: bool = False) -> dict[str, Any]:
    """Re-encode PNGs with maximum lossless compression. A file is only
    replaced when the new file decodes to exactly the same pixels and is
    smaller. ICC profiles and text chunks are carried over."""
    from io import BytesIO

    from PIL import Image, PngImagePlugin

    files, _ = _expand(paths)
    results, saved = [], 0
    for f in files:
        if f.suffix.lower() != ".png" or not f.is_file():
            continue
        original = f.read_bytes()
        try:
            with Image.open(BytesIO(original)) as im:
                im.load()
                info = PngImagePlugin.PngInfo()
                for k, v in (im.text or {}).items():
                    info.add_text(k, v)
                buf = BytesIO()
                kwargs: dict[str, Any] = {"optimize": True, "pnginfo": info}
                if im.info.get("icc_profile"):
                    kwargs["icc_profile"] = im.info["icc_profile"]
                if "transparency" in im.info:
                    kwargs["transparency"] = im.info["transparency"]
                im.save(buf, format="PNG", **kwargs)
                with Image.open(BytesIO(buf.getvalue())) as check:
                    check.load()
                    identical = (check.mode == im.mode and check.size == im.size
                                 and check.tobytes() == im.tobytes()
                                 and (im.mode != "P" or check.getpalette() == im.getpalette()))
        except Exception as exc:  # noqa: BLE001 - report per file
            results.append({"path": str(f), "status": "skipped", "reason": str(exc)})
            continue
        new = buf.getvalue()
        if not identical:
            results.append({"path": str(f), "status": "skipped", "reason": "re-encoded pixels differ"})
            continue
        if len(new) >= len(original):
            results.append({"path": str(f), "status": "already_optimal", "size": len(original)})
            continue
        gain = len(original) - len(new)
        saved += gain
        entry = {"path": str(f), "status": "smaller", "before": len(original), "after": len(new), "saved": gain}
        if apply:
            fd, tmp = tempfile.mkstemp(prefix=".png-", dir=str(f.parent))
            with os.fdopen(fd, "wb") as out:
                out.write(new)
            os.replace(tmp, f)
            entry["status"] = "optimized"
        results.append(entry)
    return {"applied": apply, "files": results, "bytes_saved": saved,
            "note": "Lossless only: pixels are verified identical before any file is replaced."}


# --------------------------------------------------------------------------
# Reversible archives
# --------------------------------------------------------------------------

def archive_create(project_id: str) -> dict[str, Any]:
    import json
    import zipfile

    roots = [r for r in project_roots() if r["id"] == project_id]
    if not roots:
        raise ValueError(f"No registered project with id '{project_id}'.")
    root = Path(roots[0]["path"])
    files = iter_project_files(roots[0], include_binary=True)
    out_dir = get_cie_home() / "archives"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"{project_id}_{stamp}.zip"
    manifest = {}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            rel = f.relative_to(root).as_posix()
            sha, _ = _hash_file(f)
            manifest[rel] = sha
            ctype = zipfile.ZIP_STORED if f.suffix.lower() in PRECOMPRESSED_EXTS else zipfile.ZIP_DEFLATED
            zf.write(f, rel, compress_type=ctype)
        zf.writestr("CIE_ARCHIVE_MANIFEST.json", json.dumps({"project_id": project_id, "root": str(root), "files": manifest}, indent=1))
    # Verify every entry round-trips before reporting success.
    with zipfile.ZipFile(target) as zf:
        for rel, sha in manifest.items():
            if hashlib.sha256(zf.read(rel)).hexdigest() != sha:
                target.unlink(missing_ok=True)
                raise RuntimeError(f"Archive verification failed for {rel}; archive removed.")
    return {"archive": str(target), "files": len(manifest), "bytes": target.stat().st_size, "verified": True,
            "note": "Project files were not changed or removed."}


def archive_restore(archive: str, target_dir: str) -> dict[str, Any]:
    import json
    import zipfile

    src = Path(archive)
    if not src.is_file() or src.resolve().parent != (get_cie_home() / "archives").resolve():
        raise ValueError("Archive must be a file in CIE's archives folder.")
    dest = Path(target_dir).expanduser()
    if not dest.is_absolute():
        raise ValueError("Target folder must be an absolute path.")
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("Target folder must be new or empty; archives never overwrite files.")
    dest.mkdir(parents=True, exist_ok=True)
    restored = 0
    with zipfile.ZipFile(src) as zf:
        manifest = json.loads(zf.read("CIE_ARCHIVE_MANIFEST.json"))["files"]
        for rel, sha in manifest.items():
            out = (dest / rel).resolve()
            if dest.resolve() not in out.parents:
                raise ValueError(f"Unsafe path in archive: {rel}")
            data = zf.read(rel)
            if hashlib.sha256(data).hexdigest() != sha:
                raise RuntimeError(f"Archive entry {rel} failed its integrity check.")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            restored += 1
    return {"restored_to": str(dest), "files": restored, "verified": True}


# --------------------------------------------------------------------------
# Auto-snapshot hook (called by the registry before write/exec capabilities)
# --------------------------------------------------------------------------

PATH_ARG_KEYS = (
    "path", "paths", "output", "output_path", "output_dir", "out_png", "out_json",
    "sheet_png", "sheet_json", "target", "dest", "destination",
)
# These only change CIE's own config, vault, or store (or snapshot for
# themselves, like storage.restore), so an extra project snapshot adds nothing.
SKIP_AUTO_SNAPSHOT_PREFIXES = ("project.", "memory.", "workflow.", "core.", "models.", "knowledge.", "routing.")
SKIP_AUTO_SNAPSHOT_IDS = {
    "coding.record_fix", "storage.restore", "storage.prune_apply", "storage.archive_restore",
    "templates.save",  # writes CIE's template library only
    "templates.apply",  # takes its own snapshot that also records the new files
}


def auto_snapshot_for(capability_id: str, permission: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Snapshot the project files a write/exec capability may touch.
    Returns snapshot info, or None when nothing in a registered project is involved."""
    if permission not in {"write", "exec"}:
        return None
    if capability_id in SKIP_AUTO_SNAPSHOT_IDS or capability_id.startswith(SKIP_AUTO_SNAPSHOT_PREFIXES):
        return None
    if not storage_settings()["auto_snapshot"]:
        return None
    candidates: list[str] = []
    for key in PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
        elif isinstance(value, list):
            candidates.extend(v for v in value if isinstance(v, str) and v.strip())
    in_project: list[str] = []
    for c in candidates:
        try:
            resolve_in_projects(c)
            in_project.append(c)
        except PermissionError:
            continue
    project_id = str(args.get("project_id") or "")
    if permission == "exec" and not in_project:
        # Scripts and test runs can touch any file: snapshot the whole project.
        script = args.get("script") or args.get("patch") or args.get("project_path")
        if isinstance(script, str) and script.strip():
            try:
                _, root = resolve_in_projects(script)
                project_id = root["id"]
            except PermissionError:
                pass
        if project_id:
            return create_snapshot(project_id=project_id, kind="auto", description=f"Before {capability_id}")
        return None
    if not in_project:
        return None
    return create_snapshot(paths=in_project, kind="auto", description=f"Before {capability_id}")


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def _wrap(fn: Any, summary: str) -> Any:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"status": "ok", "summary": summary, "data": fn(args)}
        except (ValueError, FileNotFoundError, PermissionError, RuntimeError) as exc:
            return {"status": "error", "summary": str(exc)}
    return handler


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    # Snapshots only read project files and write CIE's own store, so they run
    # without approval; that is what lets CIE snapshot before every change.
    reg.register(CapabilitySpec(
        id="storage.snapshot",
        area="storage",
        summary="Snapshot files or a whole project into CIE's deduplicated store. Reads project files; writes only CIE's own store.",
        input_schema={"paths": "optional list[str]", "project_id": "optional str", "name": "optional str", "description": "optional str"},
        permission="read",
        handler=_wrap(lambda a: create_snapshot(a.get("paths"), a.get("project_id", ""), a.get("name", ""), a.get("description", ""), "manual"), "Snapshot created."),
        tags=["snapshot", "backup", "undo", "storage"],
    ))
    reg.register(CapabilitySpec(
        id="storage.snapshots",
        area="storage",
        summary="List snapshots, newest first (optionally for one project, optionally hiding automatic ones).",
        input_schema={"project_id": "optional str", "limit": "optional int", "include_auto": "optional bool"},
        permission="read",
        handler=_wrap(lambda a: list_snapshots(a.get("project_id", ""), int(a.get("limit", 20)), a.get("include_auto", True)), "Snapshots listed."),
        tags=["snapshot", "history"],
    ))
    reg.register(CapabilitySpec(
        id="storage.restore_preview",
        area="storage",
        summary="Show exactly which files a restore would overwrite, recreate, or delete. Changes nothing.",
        input_schema={"snapshot_id": "str", "remove_added": "optional bool"},
        permission="read",
        handler=_wrap(lambda a: preview_restore(a["snapshot_id"], a.get("remove_added", False)), "Restore preview ready."),
        tags=["snapshot", "rollback", "preview"],
    ))
    reg.register(CapabilitySpec(
        id="storage.restore",
        area="storage",
        summary="Roll files back to a snapshot. Takes a safety snapshot of the current state first, so it can be undone.",
        input_schema={"snapshot_id": "str", "remove_added": "optional bool"},
        permission="write",
        destructive=True,
        handler=_wrap(lambda a: restore_snapshot(a["snapshot_id"], a.get("remove_added", False)), "Snapshot restored."),
        tags=["snapshot", "rollback", "undo"],
    ))
    reg.register(CapabilitySpec(
        id="storage.name_snapshot",
        area="storage",
        summary="Name and pin a snapshot so retention never removes it.",
        input_schema={"snapshot_id": "str", "name": "str", "pinned": "optional bool"},
        permission="read",
        handler=_wrap(lambda a: name_snapshot(a["snapshot_id"], a["name"], a.get("pinned", True)), "Snapshot named."),
        tags=["snapshot"],
    ))
    reg.register(CapabilitySpec(
        id="storage.compare",
        area="storage",
        summary="Compare two snapshots, or a snapshot against current files: added, removed, modified, and bounded text diffs.",
        input_schema={"snapshot_a": "str", "snapshot_b": "optional str", "max_diff_lines": "optional int"},
        permission="read",
        handler=_wrap(lambda a: compare(a["snapshot_a"], a.get("snapshot_b", ""), int(a.get("max_diff_lines", 60))), "Comparison ready."),
        tags=["snapshot", "diff", "versions"],
    ))
    reg.register(CapabilitySpec(
        id="storage.report",
        area="storage",
        summary="Disk use of CIE (store, snapshots, index, cache, models, archives), dedupe savings, and the 5 GB limit status.",
        input_schema={},
        permission="read",
        handler=_wrap(lambda a: storage_report(), "Storage report ready."),
        tags=["storage", "usage"],
    ))
    reg.register(CapabilitySpec(
        id="storage.prune",
        area="storage",
        summary="Apply retention: remove unnamed snapshots older than the window and unused blobs. dry_run (default) only reports.",
        input_schema={"dry_run": "optional bool"},
        permission="read",
        handler=_wrap(lambda a: _prune_entry(a), "Retention checked."),
        tags=["storage", "retention", "cleanup"],
    ))
    reg.register(CapabilitySpec(
        id="storage.prune_apply",
        area="storage",
        summary="Actually delete what storage.prune reports. Removes only CIE snapshot data, never project files or named snapshots.",
        input_schema={},
        permission="write",
        destructive=True,
        handler=_wrap(lambda a: prune(dry_run=False), "Retention applied."),
        tags=["storage", "retention", "cleanup"],
    ))
    reg.register(CapabilitySpec(
        id="storage.duplicates",
        area="storage",
        summary="Report duplicate files and the largest files in a project. Report only; changes nothing.",
        input_schema={"project_id": "str", "top": "optional int"},
        permission="read",
        handler=_wrap(lambda a: duplicate_report(a["project_id"], int(a.get("top", 20))), "Duplicate report ready."),
        tags=["storage", "duplicates", "dedupe"],
    ))
    reg.register(CapabilitySpec(
        id="storage.optimize_png",
        area="storage",
        summary="Losslessly recompress PNGs (pixels verified identical). apply=false reports savings only.",
        input_schema={"paths": "list[str]", "apply": "optional bool"},
        permission="write",
        handler=_wrap(lambda a: optimize_png(a["paths"], a.get("apply", False)), "PNG optimization done."),
        tags=["storage", "compression", "png", "2d"],
    ))
    reg.register(CapabilitySpec(
        id="storage.archive",
        area="storage",
        summary="Create a verified zip archive of a project in CIE's archives folder. Project files are not changed.",
        input_schema={"project_id": "str"},
        permission="read",
        handler=_wrap(lambda a: archive_create(a["project_id"]), "Archive created."),
        tags=["storage", "archive", "backup"],
    ))
    reg.register(CapabilitySpec(
        id="storage.archive_restore",
        area="storage",
        summary="Extract a CIE archive into a new or empty folder, verifying every file. Never overwrites.",
        input_schema={"archive": "str", "target_dir": "str"},
        permission="write",
        handler=_wrap(lambda a: archive_restore(a["archive"], a["target_dir"]), "Archive restored."),
        tags=["storage", "archive", "restore"],
    ))


def _prune_entry(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("dry_run", True) is False:
        raise ValueError("storage.prune only reports. Use storage.prune_apply (approval required) to delete.")
    return prune(dry_run=True)
