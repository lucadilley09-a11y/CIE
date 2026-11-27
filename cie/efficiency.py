"""Token and context efficiency engine: incremental indexing, shrink_file, FTS5 search, diff_since, dedupe_log, smart_context."""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any

from .config import iter_project_files, load_config, project_roots, resolve_in_projects, utc_now
from .storage import get_storage, sha256_bytes
from .vault import search_notes

LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c_header",
    ".hpp": "cpp_header",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".md": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".gd": "gdscript",
    ".lua": "lua",
    ".luau": "luau",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".kt": "kotlin",
    ".gradle": "gradle",
}

# Languages whose symbols and imports the unified indexer extracts.
CODE_LANGS = {
    "python", "javascript", "typescript", "csharp", "cpp", "c", "rust", "go",
    "java", "gdscript", "lua", "luau", "kotlin",
}

# Bump when the indexer extracts new kinds of data; triggers one full re-index.
INDEX_VERSION = 3  # 3: asset metadata (Phase 1.4)

TEXT_EXTS = set(LANG_MAP.keys()) | {".txt", ".ini", ".cfg", ".xml", ".html", ".css", ".sql", ".sh", ".bat", ".ps1"}


def detect_language(path: Path) -> str:
    return LANG_MAP.get(path.suffix.lower(), "text")


def incremental_index(project_id: str = "") -> dict[str, Any]:
    """Unified indexer: the single writer of files, FTS text, symbols, and
    dependencies. Re-indexes only changed files and removes deleted ones.
    A project indexed by an older INDEX_VERSION gets one full re-index."""
    from .coding import extract_dependencies, extract_symbols
    from .knowledge import ASSET_EXTS, extract_asset_meta, write_asset_meta

    cfg = load_config()
    roots = project_roots(cfg)
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    if not roots:
        return {"indexed": 0, "skipped_unchanged": 0, "removed": 0, "warnings": ["No matching project roots."]}

    storage = get_storage()
    indexed = 0
    skipped = 0
    removed = 0
    symbols_written = 0
    deps_written = 0
    migrated: list[str] = []
    errors: list[str] = []

    for root in roots:
        pid = root["id"]
        meta = storage.query("SELECT index_version FROM index_meta WHERE project_id = ?", (pid,))
        force_full = not meta or int(meta[0]["index_version"]) != INDEX_VERSION
        if force_full:
            migrated.append(pid)
        existing_rows = storage.query(
            "SELECT path, sha256, mtime_ns FROM files_index WHERE project_id = ?", (pid,)
        )
        existing = {r["path"]: r for r in existing_rows}
        seen_paths: set[str] = set()
        files = iter_project_files(root)
        # OBJ models share the ".obj" extension with compiled object files,
        # which the default file filter skips; index them as assets.
        known = {os.path.normcase(str(f)) for f in files}
        files += [f for f in iter_project_files(root, extensions={".obj"}, include_binary=True)
                  if os.path.normcase(str(f)) not in known]

        def _tx(conn: Any) -> None:
            nonlocal indexed, skipped, removed, symbols_written, deps_written
            for fpath in files:
                sp = str(fpath)
                seen_paths.add(sp)
                try:
                    st = fpath.stat()
                except OSError as exc:
                    errors.append(f"{sp}: {exc}")
                    continue
                prev = existing.get(sp)
                if not force_full and prev and prev["mtime_ns"] == st.st_mtime_ns:
                    skipped += 1
                    continue
                is_text = fpath.suffix.lower() in TEXT_EXTS and st.st_size <= 2_000_000
                if not is_text:
                    h = sha256_bytes(f"{st.st_size}:{st.st_mtime_ns}".encode())
                    content = ""
                else:
                    try:
                        raw = fpath.read_bytes()
                    except OSError as exc:
                        errors.append(f"{sp}: {exc}")
                        continue
                    h = sha256_bytes(raw)
                    if not force_full and prev and prev["sha256"] == h:
                        conn.execute(
                            "UPDATE files_index SET mtime_ns = ?, indexed_at = ? WHERE path = ?",
                            (st.st_mtime_ns, utc_now(), sp),
                        )
                        skipped += 1
                        continue
                    content = raw.decode("utf-8", errors="replace")

                lang = detect_language(fpath)
                conn.execute(
                    """
                    INSERT INTO files_index(path, project_id, sha256, mtime_ns, size_bytes, language, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        project_id = excluded.project_id,
                        sha256 = excluded.sha256,
                        mtime_ns = excluded.mtime_ns,
                        size_bytes = excluded.size_bytes,
                        language = excluded.language,
                        indexed_at = excluded.indexed_at
                    """,
                    (sp, pid, h, st.st_mtime_ns, st.st_size, lang, utc_now()),
                )
                conn.execute("DELETE FROM files_fts WHERE file_path = ?", (sp,))
                conn.execute("DELETE FROM symbols_index WHERE file_path = ?", (sp,))
                conn.execute("DELETE FROM dependencies_index WHERE source_file = ?", (sp,))
                if fpath.suffix.lower() in ASSET_EXTS:
                    write_asset_meta(conn, sp, pid, extract_asset_meta(fpath))
                if content:
                    conn.execute(
                        "INSERT INTO files_fts(file_path, project_id, content) VALUES (?, ?, ?)",
                        (sp, pid, content),
                    )
                    if lang in CODE_LANGS:
                        try:
                            for sym in extract_symbols(fpath, content):
                                conn.execute(
                                    "INSERT INTO symbols_index(file_path, project_id, name, kind, line_start, line_end) VALUES (?, ?, ?, ?, ?, ?)",
                                    (sp, pid, sym["name"], sym["kind"], sym["line_start"], sym["line_end"]),
                                )
                                symbols_written += 1
                            for dep in extract_dependencies(fpath, content):
                                conn.execute(
                                    "INSERT INTO dependencies_index(source_file, target, kind, confidence, project_id) VALUES (?, ?, ?, ?, ?)",
                                    (sp, dep["target"], dep["kind"], dep["confidence"], pid),
                                )
                                deps_written += 1
                        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the index
                            errors.append(f"{sp}: {type(exc).__name__}: {exc}")
                indexed += 1

            for old_path in set(existing.keys()) - seen_paths:
                conn.execute("DELETE FROM files_index WHERE path = ?", (old_path,))
                conn.execute("DELETE FROM files_fts WHERE file_path = ?", (old_path,))
                conn.execute("DELETE FROM symbols_index WHERE file_path = ?", (old_path,))
                conn.execute("DELETE FROM dependencies_index WHERE source_file = ?", (old_path,))
                conn.execute("DELETE FROM asset_meta WHERE path = ?", (old_path,))
                removed += 1
            # Rows left by an earlier, unscoped writer for this project.
            conn.execute(
                "DELETE FROM symbols_index WHERE project_id = ? AND file_path NOT IN (SELECT path FROM files_index)", (pid,)
            )
            conn.execute(
                "INSERT INTO index_meta(project_id, index_version, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET index_version = excluded.index_version, updated_at = excluded.updated_at",
                (pid, INDEX_VERSION, utc_now()),
            )

        storage.run_write(_tx)

    result: dict[str, Any] = {
        "indexed": indexed,
        "skipped_unchanged": skipped,
        "removed": removed,
        "symbols_written": symbols_written,
        "dependencies_written": deps_written,
    }
    if migrated:
        result["full_reindex"] = migrated
    if errors:
        result["errors"] = errors[:20]
        result["error_count"] = len(errors)
    return result


def search_files(query: str, project_id: str = "", limit: int = 20) -> dict[str, Any]:
    incremental_index(project_id)
    storage = get_storage()
    terms = [re.sub(r"[^A-Za-z0-9_]", "", t) for t in query.split()]
    fts_query = " AND ".join(f'"{t}"' for t in terms if t)
    if not fts_query:
        return {"matches": [], "total": 0}
    sql = """
        SELECT file_path, project_id, snippet(files_fts, 2, '[', ']', '...', 20) AS snippet
        FROM files_fts
        WHERE files_fts MATCH ?
    """
    params: list[Any] = [fts_query]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    rows = storage.query(sql, tuple(params))
    return {"matches": rows, "total": len(rows)}


def read_lines(path: str, start_line: int = 1, end_line: int = 120) -> dict[str, Any]:
    resolved, root = resolve_in_projects(path)
    lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    s = max(1, int(start_line))
    e = min(len(lines), max(s, int(end_line)))
    excerpt = [{"line": idx, "text": lines[idx - 1]} for idx in range(s, e + 1)]
    return {
        "path": str(resolved),
        "project_id": root["id"],
        "total_lines": len(lines),
        "start_line": s,
        "end_line": e,
        "lines": excerpt,
    }


def shrink_file(path: str, max_lines: int = 80) -> dict[str, Any]:
    """Extract headings, symbols, imports, and key structural lines instead of dumping the whole file."""
    from .coding import extract_symbols

    resolved, root = resolve_in_projects(path)
    text = resolved.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    lang = detect_language(resolved)
    symbols = extract_symbols(resolved, text)
    headings = [
        {"line": i + 1, "text": ln.strip()}
        for i, ln in enumerate(lines)
        if ln.lstrip().startswith("#") or ln.lstrip().startswith("##")
    ][:30]
    imports = [
        {"line": i + 1, "text": ln.strip()}
        for i, ln in enumerate(lines[:60])
        if ln.lstrip().startswith(("import ", "from ", "#include", "using ", "use "))
    ][:25]
    return {
        "path": str(resolved),
        "project_id": root["id"],
        "language": lang,
        "total_lines": len(lines),
        "symbols": symbols[:max_lines],
        "headings": headings,
        "imports": imports,
    }


def file_map(project_id: str = "", max_files: int = 200) -> dict[str, Any]:
    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    mapping: list[dict[str, Any]] = []
    omitted = 0
    for r in roots:
        files = iter_project_files(r)
        if len(files) > max_files:
            omitted += len(files) - max_files
            files = files[:max_files]
        root_p = Path(r["path"]).resolve()
        entry_points = [
            str(f.relative_to(root_p)).replace("\\", "/")
            for f in files
            if f.name in {"main.py", "app.py", "mcp_server.py", "index.js", "index.ts", "main.rs", "main.go", "Program.cs"}
        ]
        mapping.append({
            "project_id": r["id"],
            "root": str(root_p),
            "entry_points": entry_points,
            "files": [str(f.relative_to(root_p)).replace("\\", "/") for f in files],
        })
    return {"projects": mapping, "omitted_files": omitted}


def dedupe_log(log_text: str, max_groups: int = 40) -> dict[str, Any]:
    lines = log_text.splitlines()
    groups: dict[str, dict[str, Any]] = {}
    for idx, raw in enumerate(lines, start=1):
        norm = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?", "<TS>", raw.strip())
        norm = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", norm)
        if not norm:
            continue
        if norm not in groups:
            groups[norm] = {"pattern": raw.strip(), "count": 1, "first_line": idx, "last_line": idx}
        else:
            groups[norm]["count"] += 1
            groups[norm]["last_line"] = idx
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["first_line"]))
    return {
        "total_lines": len(lines),
        "unique_groups": len(ranked),
        "groups": ranked[:max_groups],
        "omitted_groups": max(0, len(ranked) - max_groups),
    }


def errors_only(log_text: str, max_items: int = 50) -> dict[str, Any]:
    lines = log_text.splitlines()
    hits = [
        {"line": i + 1, "text": ln.strip()}
        for i, ln in enumerate(lines)
        if re.search(r"\b(error|exception|traceback|failed|fatal|warning|warn)\b", ln, re.IGNORECASE)
    ]
    return {
        "total_lines": len(lines),
        "matched_lines": len(hits),
        "items": hits[:max_items],
        "omitted": max(0, len(hits) - max_items),
    }


def diff_since(path_a: str, path_b: str | None = None, baseline_text: str | None = None) -> dict[str, Any]:
    resolved_a, _ = resolve_in_projects(path_a)
    text_a = resolved_a.read_text(encoding="utf-8", errors="replace").splitlines()
    if path_b:
        resolved_b, _ = resolve_in_projects(path_b)
        text_b = resolved_b.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(difflib.unified_diff(text_a, text_b, fromfile=str(resolved_a), tofile=str(resolved_b), lineterm=""))
    elif baseline_text is not None:
        diff = list(difflib.unified_diff(baseline_text.splitlines(), text_a, fromfile="baseline", tofile=str(resolved_a), lineterm=""))
    else:
        return {"changed": False, "diff": []}
    return {"changed": bool(diff), "diff_lines": len(diff), "diff": diff[:200]}


def _estimate_tokens(text: str) -> tuple[int, str]:
    """Estimate model tokens without requiring a network or tokenizer dependency."""
    try:
        import tiktoken  # type: ignore
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)), "tiktoken:cl100k_base"
    except Exception:
        # Conservative multilingual fallback; this is an estimate, not an exact count.
        import math
        return math.ceil(len(text.encode("utf-8")) / 3.0), "utf8_bytes_div_3_estimate"


def smart_context(
    task: str,
    project_id: str = "",
    budget_bytes: int | None = None,
    budget_tokens: int | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    if budget_tokens is not None:
        cap_tokens = max(128, min(int(budget_tokens), 100_000))
    elif budget_bytes is not None:
        # Backwards compatibility for existing clients using the former byte budget.
        cap_tokens = max(128, min(int(budget_bytes) // 3, 100_000))
    else:
        cap_tokens = max(128, min(int(cfg.get("context_budget_tokens", 6000)), 100_000))

    file_hits = search_files(task, project_id=project_id, limit=8).get("matches", [])
    note_hits = search_notes(task, project=project_id, limit=5)
    bundle: dict[str, Any] = {
        "task": task, "budget_tokens": cap_tokens, "token_estimator": "",
        "file_hits": [], "memory_hits": [], "omitted": 0,
    }
    used_tokens = 0
    estimator = "utf8_bytes_div_3_estimate"
    # Include task framing in the budget because it is also sent to the model.
    task_tokens, estimator = _estimate_tokens(task)
    used_tokens += task_tokens
    for group_name, items in (("file_hits", file_hits), ("memory_hits", note_hits)):
        for item in items:
            item_tokens, method = _estimate_tokens(str(item))
            estimator = method
            if used_tokens + item_tokens > cap_tokens:
                bundle["omitted"] += 1
                continue
            bundle[group_name].append(item)
            used_tokens += item_tokens
    bundle["used_tokens"] = used_tokens
    bundle["token_estimator"] = estimator
    bundle["budget_utilization"] = round(used_tokens / cap_tokens, 4) if cap_tokens else 0.0
    return bundle


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="eff.incremental_index",
        area="efficiency",
        summary="Incrementally index approved project folders into SQLite FTS5.",
        input_schema={"project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Incremental index updated.", "data": incremental_index(args.get("project_id", ""))},
        tags=["index", "fts5"],
    ))
    reg.register(CapabilitySpec(
        id="eff.search_files",
        area="efficiency",
        summary="Search approved text files via FTS5 and return ranked snippets.",
        input_schema={"query": "str", "project_id": "optional str", "limit": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Search completed.", "data": search_files(args.get("query", ""), args.get("project_id", ""), int(args.get("limit", 20)))},
        tags=["search", "fts5"],
    ))
    reg.register(CapabilitySpec(
        id="eff.shrink_file",
        area="efficiency",
        summary="Extract headings, imports, and symbols from a file without loading all lines.",
        input_schema={"path": "str", "max_lines": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "File structure extracted.", "data": shrink_file(args["path"], int(args.get("max_lines", 80)))},
        tags=["shrink", "outline"],
    ))
    reg.register(CapabilitySpec(
        id="eff.file_map",
        area="efficiency",
        summary="List project tree, entry points, and relative file paths.",
        input_schema={"project_id": "optional str", "max_files": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Project file map built.", "data": file_map(args.get("project_id", ""), int(args.get("max_files", 200)))},
        tags=["map", "tree"],
    ))
    reg.register(CapabilitySpec(
        id="eff.read_lines",
        area="efficiency",
        summary="Read exact line ranges from an approved file.",
        input_schema={"path": "str", "start_line": "int", "end_line": "int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Line window read.", "data": read_lines(args["path"], int(args.get("start_line", 1)), int(args.get("end_line", 120)))},
        tags=["read", "lines"],
    ))
    reg.register(CapabilitySpec(
        id="eff.dedupe_log",
        area="efficiency",
        summary="Group repetitive log lines with counts and first/last occurrence.",
        input_schema={"log_text": "str", "max_groups": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Log deduplicated.", "data": dedupe_log(args.get("log_text", ""), int(args.get("max_groups", 40)))},
        tags=["logs", "dedupe"],
    ))
    reg.register(CapabilitySpec(
        id="eff.errors_only",
        area="efficiency",
        summary="Extract only errors, tracebacks, and warnings from log output.",
        input_schema={"log_text": "str", "max_items": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Errors extracted.", "data": errors_only(args.get("log_text", ""), int(args.get("max_items", 50)))},
        tags=["logs", "errors"],
    ))
    reg.register(CapabilitySpec(
        id="eff.diff_since",
        area="efficiency",
        summary="Compute compact unified diff between two files or against a baseline.",
        input_schema={"path_a": "str", "path_b": "optional str", "baseline_text": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Diff computed.", "data": diff_since(args["path_a"], args.get("path_b"), args.get("baseline_text"))},
        tags=["diff"],
    ))
    reg.register(CapabilitySpec(
        id="eff.smart_context",
        area="efficiency",
        summary="Combine task-relevant files and memory notes under a strict byte budget.",
        input_schema={"task": "str", "project_id": "optional str", "budget_tokens": "optional int", "budget_bytes": "legacy optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Smart context assembled with an explicit token budget.", "data": smart_context(args.get("task", ""), args.get("project_id", ""), args.get("budget_bytes"), args.get("budget_tokens"))},
        tags=["context", "budget"],
    ))
    reg.register(CapabilitySpec(
        id="eff.cache_inspector",
        area="efficiency",
        summary="Inspect result cache entries, hit counts, and storage size.",
        input_schema={},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Cache stats retrieved.", "data": get_storage().cache_stats()},
        tags=["cache"],
    ))