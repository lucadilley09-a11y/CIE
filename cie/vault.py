"""Obsidian-compatible Markdown memory vault with wikilinks, backlinks, FTS5 search, and version history."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

from .config import get_vault_root, redact_secrets, utc_now
from .storage import get_storage, sha256_text

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

VAULT_DIRS = [
    "00-Index",
    "Projects",
    "Knowledge/Coding",
    "Knowledge/Art",
    "Knowledge/Audio",
    "Knowledge/Tools-and-APIs",
    "Fixes",
    "Research",
    "Preferences",
    "Daily",
    "Workflows",
    "Templates",
    "Attachments",
    ".cie/cache",
    ".cie/history",
]


def ensure_vault_layout(vault: Path | None = None) -> Path:
    # Canonicalize symlinked platform temp paths (notably macOS /var -> /private/var)
    # before any relative_to checks or writes.
    v = (vault or get_vault_root()).resolve()
    for rel in VAULT_DIRS:
        (v / rel).mkdir(parents=True, exist_ok=True)
    # Seed 00-Index files only if missing (never overwrite user edits)
    seeds = {
        "00-Index/Home.md": "# CIE Vault Home\n\n- [[Projects]]\n- [[Knowledge-Map]]\n",
        "00-Index/Projects.md": "# Projects\n\nProject notes live under `Projects/<Project>/`.\n",
        "00-Index/Knowledge-Map.md": "# Knowledge Map\n\n- Coding\n- Art\n- Audio\n- Tools-and-APIs\n",
    }
    for rel, content in seeds.items():
        p = v / rel
        if not p.exists():
            p.write_text(content, encoding="utf-8")
    return v


def _safe_slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip().strip(".")
    return cleaned or f"note-{uuid.uuid4().hex[:8]}"


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            fm_text = raw[4:end]
            body = raw[end + 5:]
            try:
                meta = yaml.safe_load(fm_text) or {}
                if isinstance(meta, dict):
                    return meta, body
            except Exception:
                pass
    return {}, raw


def _render_note(meta: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.lstrip()}"


def _history_dir(vault: Path, rel_path: str) -> Path:
    root = vault.resolve()
    normalized_path = rel_path.replace(chr(92), "/")
    candidate = vault / ".cie" / "history" / _safe_slug(normalized_path.replace("/", "__"))
    # Refuse symlinks in any history path component, even if they point back inside.
    for component in (vault / ".cie", vault / ".cie" / "history", candidate):
        if component.is_symlink():
            raise PermissionError("Memory history symlinks are not permitted.")
    candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError("Memory history path escaped the vault.")
    return resolved


def _save_version(vault: Path, rel_path: str, old_content: str) -> str:
    hist_dir = _history_dir(vault, rel_path)
    ts = utc_now().replace(":", "-")
    snap = hist_dir / f"{ts}_{uuid.uuid4().hex[:6]}.md"
    snap.write_text(old_content, encoding="utf-8")
    return str(snap)


def list_note_versions(rel_path: str) -> list[dict[str, Any]]:
    vault = ensure_vault_layout()
    hist_dir = _history_dir(vault, rel_path)
    items = []
    for p in sorted(hist_dir.glob("*.md"), reverse=True):
        if p.is_file() and not p.is_symlink():
            items.append({"version_file": p.name, "path": str(p), "size_bytes": p.stat().st_size})
    return items


def restore_note_version(rel_path: str, version_file: str) -> dict[str, Any]:
    vault = ensure_vault_layout()
    target = (vault / rel_path).resolve()
    if not target.is_relative_to(vault.resolve()):
        raise PermissionError("Invalid note path.")
    if not version_file or Path(version_file).name != version_file or not version_file.endswith(".md"):
        raise PermissionError("Invalid version filename.")
    hist_dir = _history_dir(vault, rel_path)
    hist_candidate = hist_dir / version_file
    if hist_candidate.is_symlink():
        raise PermissionError("Version history symlinks are not permitted.")
    hist_file = hist_candidate.resolve()
    if not hist_file.is_relative_to(hist_dir):
        raise PermissionError("Version path must stay inside the note history folder.")
    if not hist_file.exists() or not hist_file.is_file():
        raise FileNotFoundError(f"Version '{version_file}' not found.")
    if target.exists():
        _save_version(vault, rel_path, target.read_text(encoding="utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(hist_file.read_text(encoding="utf-8"), encoding="utf-8")
    sync_vault_index()
    return {"restored": rel_path, "from_version": version_file}


def save_note(
    title: str,
    body: str,
    folder: str = "Knowledge/Coding",
    project: str = "",
    tags: list[str] | None = None,
    source_type: str = "cie_observed",
    source_path: str = "",
    confidence: str = "observed",
    links: list[str] | None = None,
    rel_path: str | None = None,
    replace_tags: bool = False,
) -> dict[str, Any]:
    vault = ensure_vault_layout()
    clean_body = redact_secrets(body)
    safe_folder = folder.strip("/").replace("..", "")
    if rel_path:
        target = (vault / rel_path.lstrip("/")).resolve()
    else:
        target = (vault / safe_folder / f"{_safe_slug(title)}.md").resolve()
    if not target.is_relative_to(vault.resolve()):
        raise PermissionError("Note path must stay inside CIE-Vault.")
    if ".obsidian" in target.parts:
        raise PermissionError("Cannot write inside .obsidian directory.")

    now = utc_now()
    existing_meta: dict[str, Any] = {}
    if target.exists():
        old_raw = target.read_text(encoding="utf-8", errors="replace")
        _save_version(vault, str(target.relative_to(vault)), old_raw)
        existing_meta, _ = _parse_frontmatter(old_raw)

    meta = {
        "id": existing_meta.get("id") or uuid.uuid4().hex[:12],
        "title": title,
        "created": existing_meta.get("created") or now,
        "updated": now,
        "project": project or existing_meta.get("project", ""),
        "tags": sorted(set(tags or [])) if replace_tags else sorted(set((existing_meta.get("tags") or []) + (tags or []))),
        "source_type": source_type,
        "source_path": source_path,
        "confidence": confidence,
        "last_reviewed": now,
        "links": links or existing_meta.get("links", []),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_note(meta, clean_body), encoding="utf-8")
    sync_vault_index()
    rel = str(target.relative_to(vault)).replace("\\", "/")
    return {"rel_path": rel, "id": meta["id"], "title": title, "updated": now}


def append_daily_note(entry: str, date_str: str | None = None) -> dict[str, Any]:
    vault = ensure_vault_layout()
    day = date_str or utc_now()[:10]
    rel = f"Daily/{day}.md"
    p = vault / rel
    clean = redact_secrets(entry).strip()
    if p.exists():
        old = p.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(old)
        new_body = f"{body.rstrip()}\n- [{utc_now()[11:19]}Z] {clean}\n"
        return save_note(
            title=meta.get("title", f"Daily {day}"),
            body=new_body,
            folder="Daily",
            tags=meta.get("tags") or ["daily"],
            source_type="cie_observed",
            confidence="observed",
            rel_path=rel,
        )
    return save_note(
        title=f"Daily {day}",
        body=f"# Daily Log — {day}\n\n- [{utc_now()[11:19]}Z] {clean}\n",
        folder="Daily",
        tags=["daily"],
        source_type="cie_observed",
        confidence="observed",
        rel_path=rel,
    )


def sync_vault_index() -> dict[str, Any]:
    """Scan Markdown notes in the vault, detect external Obsidian edits, and update SQLite FTS5 + backlinks."""
    vault = ensure_vault_layout()
    storage = get_storage()
    md_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if d not in {".cie", ".obsidian", ".git"}]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                md_files.append(Path(dirpath) / fn)

    title_to_rel: dict[str, str] = {}
    parsed_notes: list[dict[str, Any]] = []
    for p in sorted(md_files):
        rel = str(p.relative_to(vault)).replace("\\", "/")
        raw = p.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(raw)
        title = str(meta.get("title") or p.stem)
        title_to_rel[title.lower()] = rel
        title_to_rel[p.stem.lower()] = rel
        parsed_notes.append({
            "rel_path": rel,
            "note_id": str(meta.get("id") or sha256_text(rel)[:12]),
            "title": title,
            "note_type": rel.split("/")[0] if "/" in rel else "Root",
            "project": str(meta.get("project") or ""),
            "status": str(meta.get("status") or "active"),
            "confidence": str(meta.get("confidence") or "observed"),
            "tags": " ".join(str(t) for t in (meta.get("tags") or [])),
            "updated_at": str(meta.get("updated") or utc_now()),
            "sha256": sha256_text(raw),
            "body": body,
            "wikilinks": WIKILINK_RE.findall(body),
        })

    def _tx(conn: Any) -> None:
        conn.execute("DELETE FROM notes_index")
        conn.execute("DELETE FROM notes_links")
        conn.execute("DELETE FROM notes_fts")
        for n in parsed_notes:
            conn.execute(
                """
                INSERT INTO notes_index(rel_path, note_id, title, note_type, project, status, confidence, tags, updated_at, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    n["rel_path"], n["note_id"], n["title"], n["note_type"],
                    n["project"], n["status"], n["confidence"], n["tags"],
                    n["updated_at"], n["sha256"],
                ),
            )
            conn.execute(
                "INSERT INTO notes_fts(rel_path, title, tags, body) VALUES (?, ?, ?, ?)",
                (n["rel_path"], n["title"], n["tags"], n["body"]),
            )
            for target_title in n["wikilinks"]:
                resolved = title_to_rel.get(target_title.strip().lower(), "")
                conn.execute(
                    "INSERT INTO notes_links(source_path, target_title, target_path, is_resolved) VALUES (?, ?, ?, ?)",
                    (n["rel_path"], target_title.strip(), resolved, 1 if resolved else 0),
                )

    storage.run_write(_tx)
    return {"notes_indexed": len(parsed_notes)}


def search_notes(query: str, project: str = "", limit: int = 10) -> list[dict[str, Any]]:
    sync_vault_index()
    storage = get_storage()
    q = query.strip()
    if not q:
        sql = "SELECT rel_path, note_id, title, note_type, project, confidence, tags, updated_at FROM notes_index"
        params: tuple[Any, ...] = ()
        if project:
            sql += " WHERE project = ?"
            params = (project,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params = params + (limit,)
        return storage.query(sql, params)

    # Escape FTS5 special tokens safely
    terms = [re.sub(r"[^A-Za-z0-9_]", "", t) for t in q.split()]
    fts_query = " OR ".join(f'"{t}"' for t in terms if t)
    if not fts_query:
        return []
    rows = storage.query(
        """
        SELECT f.rel_path, n.title, n.project, n.confidence, n.tags,
               snippet(notes_fts, 3, '[', ']', '...', 18) AS snippet
        FROM notes_fts f
        JOIN notes_index n ON n.rel_path = f.rel_path
        WHERE notes_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit),
    )
    if project:
        rows = [r for r in rows if not r.get("project") or r["project"] == project]
    return rows


def get_backlinks_and_mentions(note_title_or_path: str) -> dict[str, Any]:
    sync_vault_index()
    storage = get_storage()
    vault = ensure_vault_layout()
    target_row = storage.query(
        "SELECT rel_path, title FROM notes_index WHERE rel_path = ? OR LOWER(title) = LOWER(?)",
        (note_title_or_path, note_title_or_path),
    )
    title = target_row[0]["title"] if target_row else Path(note_title_or_path).stem
    rel_path = target_row[0]["rel_path"] if target_row else note_title_or_path

    backlinks = storage.query(
        "SELECT source_path, target_title FROM notes_links WHERE target_path = ? OR LOWER(target_title) = LOWER(?)",
        (rel_path, title),
    )
    # Find unlinked mentions
    unlinked: list[dict[str, str]] = []
    linked_sources = {b["source_path"] for b in backlinks}
    if len(title) >= 3:
        pat = re.compile(rf"\b{re.escape(title)}\b", re.IGNORECASE)
        for row in storage.query("SELECT rel_path FROM notes_index"):
            src = row["rel_path"]
            if src == rel_path or src in linked_sources:
                continue
            p = vault / src
            if p.exists():
                txt = p.read_text(encoding="utf-8", errors="replace")
                if pat.search(txt):
                    unlinked.append({"source_path": src, "suggested_link": f"[[{title}]]"})
    return {"title": title, "rel_path": rel_path, "backlinks": backlinks, "unlinked_mentions": unlinked}


def review_vault() -> dict[str, Any]:
    """Flag duplicate titles, unresolved wikilinks, and unverified notes without auto-merging."""
    sync_vault_index()
    storage = get_storage()
    notes = storage.query("SELECT rel_path, title, confidence, updated_at FROM notes_index")
    by_title: dict[str, list[str]] = {}
    for n in notes:
        by_title.setdefault(n["title"].lower(), []).append(n["rel_path"])
    duplicates = [{"title": t, "paths": ps} for t, ps in by_title.items() if len(ps) > 1]
    unresolved = storage.query("SELECT source_path, target_title FROM notes_links WHERE is_resolved = 0")
    unverified = [n for n in notes if n["confidence"] in ("inferred", "unverified")]
    return {
        "total_notes": len(notes),
        "duplicate_titles": duplicates,
        "unresolved_wikilinks": unresolved,
        "needs_verification": unverified,
    }


def export_vault(dest_dir: str) -> dict[str, Any]:
    vault = ensure_vault_layout().resolve()
    dest = Path(dest_dir).expanduser().resolve()
    # Prevent recursive copies and exporting into the source vault itself.
    if dest == vault or dest.is_relative_to(vault) or vault.is_relative_to(dest):
        raise PermissionError("Export destination must not overlap the memory vault.")
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for dirpath, dirnames, filenames in os.walk(vault, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in {".cie", ".obsidian"} and not (current / d).is_symlink()]
        rel_dir = current.relative_to(vault)
        out_dir = (dest / rel_dir).resolve()
        if not out_dir.is_relative_to(dest):
            raise PermissionError("Export path escaped its destination.")
        out_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            source = current / fn
            if fn.endswith(".md") and source.is_file() and not source.is_symlink():
                target = out_dir / fn
                if target.exists() and target.is_symlink():
                    raise PermissionError("Export refuses to overwrite a symlink.")
                shutil.copy2(source, target)
                copied += 1
    return {"exported_to": str(dest), "files_copied": copied}


def import_vault(src_dir: str) -> dict[str, Any]:
    src = Path(src_dir).expanduser().resolve()
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Source vault folder not found: {src_dir}")
    vault = ensure_vault_layout().resolve()
    if src == vault or src.is_relative_to(vault) or vault.is_relative_to(src):
        raise PermissionError("Import source must not overlap the active memory vault.")
    imported = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in {".cie", ".obsidian", ".git"} and not (current / d).is_symlink()]
        rel_dir = current.relative_to(src)
        out_dir = (vault / rel_dir).resolve()
        if not out_dir.is_relative_to(vault):
            raise PermissionError("Import path escaped the memory vault.")
        out_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            source = current / fn
            if not (fn.endswith(".md") and source.is_file() and not source.is_symlink()):
                continue
            target = (out_dir / fn).resolve()
            if not target.is_relative_to(vault) or (out_dir / fn).is_symlink():
                raise PermissionError("Import refuses to write through a symlink.")
            shutil.copy2(source, target)
            imported += 1
    sync_info = sync_vault_index()
    return {"imported_files": imported, **sync_info}


def register_capabilities(reg: Any) -> None:
    """Expose memory operations through the same permission registry as other tools."""
    from .registry import CapabilitySpec

    actions = [
        ("memory.search", "Search memory notes", "read", False, {"query": "optional str", "project": "optional str", "limit": "optional int"}, lambda a: {"results": search_notes(a.get("query", ""), a.get("project", ""), int(a.get("limit", 10)))}),
        ("memory.save", "Create or update a memory note", "write", False, {"title": "str", "body": "optional str", "folder": "optional str", "project": "optional str", "tags": "optional list[str]", "confidence": "optional str", "rel_path": "optional str"}, lambda a: {"note": save_note(a["title"], a.get("body", ""), a.get("folder", "Knowledge/Coding"), a.get("project", ""), a.get("tags"), confidence=a.get("confidence", "observed"), rel_path=a.get("rel_path") or None)}),
        ("memory.daily", "Append an entry to the daily memory note", "write", False, {"body": "optional str", "title": "optional str"}, lambda a: {"note": append_daily_note(a.get("body") or a.get("title", ""))}),
        ("memory.backlinks", "Find memory backlinks and mentions", "read", False, {"rel_path": "optional str", "title": "optional str", "query": "optional str"}, lambda a: {"data": get_backlinks_and_mentions(a.get("rel_path") or a.get("title") or a.get("query", ""))}),
        ("memory.review", "Review memory vault quality", "read", False, {}, lambda a: {"data": review_vault()}),
        ("memory.versions", "List versions of a memory note", "read", False, {"rel_path": "str"}, lambda a: {"versions": list_note_versions(a.get("rel_path", ""))}),
        ("memory.restore", "Restore a previous memory note version", "write", True, {"rel_path": "str", "version_file": "str"}, lambda a: {"data": restore_note_version(a["rel_path"], a["version_file"])}),
        ("memory.export", "Export Markdown memory notes to a folder", "write", True, {"dest_or_src_dir": "str"}, lambda a: {"data": export_vault(a["dest_or_src_dir"])}),
        ("memory.import", "Import Markdown notes into the memory vault", "write", True, {"dest_or_src_dir": "str"}, lambda a: {"data": import_vault(a["dest_or_src_dir"])}),
    ]
    for cid, summary, permission, destructive, schema, handler in actions:
        reg.register(CapabilitySpec(
            id=cid, area="memory", summary=summary, input_schema=schema,
            permission=permission, destructive=destructive, handler=lambda args, fn=handler, label=summary: {"status": "ok", "summary": label, "data": fn(args)},
            tags=["memory", "vault", "obsidian"],
        ))
