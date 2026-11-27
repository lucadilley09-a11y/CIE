"""Phase 1.4 persistent project knowledge.

- Asset metadata (images, audio, 3D models) extracted during indexing and
  stored in the unified index, so Claude can ask "which sprites are not
  power-of-two" or "how many vertices does this model have" without opening
  the files.
- A background re-indexer that keeps the index current while the CIE server
  runs (mtime-based, so an idle pass is cheap).
- Structured project notes (decisions, known issues, architecture, history,
  preferences) stored as vault notes with front-matter, plus a compact
  project summary sized for Claude's context.

Only the Python standard library and Pillow (already a Tier 1 dependency)
are used.
"""

from __future__ import annotations

import json
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

from .config import load_config, project_roots, utc_now
from .storage import get_storage

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga", ".tif", ".tiff"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3", ".flac", ".m4a"}
MODEL_EXTS = {".obj", ".gltf", ".glb", ".stl", ".fbx", ".blend", ".rbxm", ".rbxl", ".rbxlx"}
ASSET_EXTS = IMAGE_EXTS | AUDIO_EXTS | MODEL_EXTS
MAX_ASSET_BYTES = 512 * 1024 * 1024

KINDS = ("decision", "issue", "architecture", "history", "preference", "reusable")
KIND_FOLDERS = {
    "decision": "Decisions", "issue": "Issues", "architecture": "Architecture",
    "history": "History", "preference": "Preferences", "reusable": "Reusable",
}
ISSUE_STATES = ("open", "fixed", "wontfix")


# --------------------------------------------------------------------------
# Asset metadata
# --------------------------------------------------------------------------

def _image_meta(path: Path) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as im:  # lazy: reads the header, not all pixels
        w, h = im.size
        frames = int(getattr(im, "n_frames", 1) or 1)
        alpha = im.mode in {"RGBA", "LA", "PA"} or "transparency" in im.info
        return {
            "kind": "image", "width": w, "height": h, "mode": im.mode,
            "frames": frames, "has_alpha": alpha,
            "power_of_two": w > 0 and h > 0 and (w & (w - 1)) == 0 and (h & (h - 1)) == 0,
        }


def _audio_meta(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.getnframes()
            return {
                "kind": "audio", "sample_rate": rate, "channels": wf.getnchannels(),
                "bit_depth": wf.getsampwidth() * 8,
                "duration_sec": round(frames / rate, 3) if rate else 0.0,
            }
    return {"kind": "audio", "format": path.suffix.lower().lstrip(".")}


def _gltf_json_meta(doc: dict[str, Any]) -> dict[str, Any]:
    accessors = doc.get("accessors") or []
    vertices = 0
    triangles = 0
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            pos = (prim.get("attributes") or {}).get("POSITION")
            if isinstance(pos, int) and 0 <= pos < len(accessors):
                vertices += int(accessors[pos].get("count") or 0)
            idx = prim.get("indices")
            if isinstance(idx, int) and 0 <= idx < len(accessors):
                triangles += int(accessors[idx].get("count") or 0) // 3
    return {
        "kind": "model", "format": "gltf",
        "meshes": len(doc.get("meshes") or []), "nodes": len(doc.get("nodes") or []),
        "materials": len(doc.get("materials") or []), "animations": len(doc.get("animations") or []),
        "skins": len(doc.get("skins") or []), "vertices": vertices, "triangles": triangles,
    }


def _model_meta(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext == ".obj":
        v = f = 0
        with path.open("rb") as fh:
            for line in fh:
                if line.startswith(b"v "):
                    v += 1
                elif line.startswith(b"f "):
                    f += 1
        return {"kind": "model", "format": "obj", "vertices": v, "faces": f}
    if ext == ".gltf":
        return _gltf_json_meta(json.loads(path.read_text(encoding="utf-8")))
    if ext == ".glb":
        with path.open("rb") as fh:
            header = fh.read(12)
            if len(header) < 12 or header[:4] != b"glTF":
                return {"kind": "model", "format": "glb", "valid_header": False}
            length, ctype = struct.unpack("<I4s", fh.read(8))
            if ctype != b"JSON":
                return {"kind": "model", "format": "glb", "valid_header": False}
            meta = _gltf_json_meta(json.loads(fh.read(length).decode("utf-8")))
            meta["format"] = "glb"
            return meta
    if ext == ".stl":
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(84)
        if len(head) == 84:
            tris = struct.unpack("<I", head[80:84])[0]
            if 84 + tris * 50 == size:
                return {"kind": "model", "format": "stl", "triangles": tris}
        count = 0
        with path.open("rb") as fh:
            for line in fh:
                if line.lstrip().startswith(b"facet"):
                    count += 1
        return {"kind": "model", "format": "stl", "triangles": count}
    return {"kind": "model", "format": ext.lstrip(".")}


def extract_asset_meta(path: Path) -> dict[str, Any] | None:
    ext = path.suffix.lower()
    if ext not in ASSET_EXTS:
        return None
    try:
        if path.stat().st_size > MAX_ASSET_BYTES:
            return {"kind": "large", "format": ext.lstrip("."), "note": "over 512 MB; not parsed"}
        if ext in IMAGE_EXTS:
            return _image_meta(path)
        if ext in AUDIO_EXTS:
            return _audio_meta(path)
        return _model_meta(path)
    except Exception as exc:  # noqa: BLE001 - a bad asset must not stop indexing
        return {"kind": "unreadable", "format": ext.lstrip("."), "error": f"{type(exc).__name__}: {exc}"[:200]}


def write_asset_meta(conn: Any, path: str, project_id: str, meta: dict[str, Any] | None) -> None:
    conn.execute("DELETE FROM asset_meta WHERE path = ?", (path,))
    if meta:
        conn.execute(
            "INSERT INTO asset_meta(path, project_id, kind, meta_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (path, project_id, meta.get("kind", "other"), json.dumps(meta, sort_keys=True), utc_now()),
        )


def asset_query(project_id: str, kind: str = "", where: dict[str, Any] | None = None, limit: int = 50) -> dict[str, Any]:
    """List assets with metadata. ``where`` filters on metadata fields,
    e.g. {"power_of_two": false} or {"has_alpha": true}."""
    from .efficiency import incremental_index

    incremental_index(project_id)
    sql = "SELECT path, kind, meta_json FROM asset_meta WHERE project_id = ?"
    params: list[Any] = [project_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    rows = get_storage().query(sql + " ORDER BY path", tuple(params))
    out = []
    for r in rows:
        meta = json.loads(r["meta_json"])
        if where and any(meta.get(k) != v for k, v in where.items()):
            continue
        out.append({"path": r["path"], **meta})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return {"project_id": project_id, "total_matching": len(out), "counts_by_kind": counts,
            "assets": out[: max(1, min(int(limit), 500))], "truncated": len(out) > limit}


# --------------------------------------------------------------------------
# Background re-indexer
# --------------------------------------------------------------------------

_STATE: dict[str, Any] = {"running": False, "interval_sec": None, "last_run": None, "last_result": None, "last_error": None, "passes": 0}
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def knowledge_settings() -> dict[str, Any]:
    settings: dict[str, Any] = {"background_index": True, "interval_sec": 60}
    raw = load_config().get("knowledge")
    if isinstance(raw, dict):
        if isinstance(raw.get("background_index"), bool):
            settings["background_index"] = raw["background_index"]
        if isinstance(raw.get("interval_sec"), int) and not isinstance(raw["interval_sec"], bool) and raw["interval_sec"] >= 10:
            settings["interval_sec"] = raw["interval_sec"]
    return settings


def run_index_pass() -> dict[str, Any]:
    from .efficiency import incremental_index

    totals = {"indexed": 0, "removed": 0, "projects": 0}
    for root in project_roots():
        if not Path(root["path"]).is_dir():
            continue
        res = incremental_index(root["id"])
        totals["indexed"] += res.get("indexed", 0)
        totals["removed"] += res.get("removed", 0)
        totals["projects"] += 1
    _STATE["last_run"] = utc_now()
    _STATE["last_result"] = totals
    _STATE["passes"] += 1
    return totals


def _loop(interval: int) -> None:
    while not _STOP.is_set():
        try:
            run_index_pass()
            _STATE["last_error"] = None
        except Exception as exc:  # noqa: BLE001 - keep the watcher alive
            _STATE["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
        _STOP.wait(interval)


def start_background_indexer() -> dict[str, Any]:
    """Start the re-indexer thread if enabled. Safe to call more than once."""
    global _THREAD
    settings = knowledge_settings()
    if not settings["background_index"]:
        return {"started": False, "reason": "disabled in config (knowledge.background_index)"}
    if _THREAD and _THREAD.is_alive():
        return {"started": False, "reason": "already running"}
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, args=(settings["interval_sec"],), name="cie-indexer", daemon=True)
    _THREAD.start()
    _STATE["running"] = True
    _STATE["interval_sec"] = settings["interval_sec"]
    return {"started": True, "interval_sec": settings["interval_sec"]}


def stop_background_indexer() -> None:
    _STOP.set()
    _STATE["running"] = False


def indexer_status() -> dict[str, Any]:
    alive = bool(_THREAD and _THREAD.is_alive())
    return {**_STATE, "running": alive, "settings": knowledge_settings()}


# --------------------------------------------------------------------------
# Structured project notes (decisions, issues, architecture, ...)
# --------------------------------------------------------------------------

def record(project: str, kind: str, title: str, body: str = "", status: str = "", tags: list[str] | None = None,
           _replace_tags: bool = False) -> dict[str, Any]:
    from .vault import save_note

    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if not project.strip() or not title.strip():
        raise ValueError("project and title are required.")
    if kind == "issue":
        status = status or "open"
        if status not in ISSUE_STATES:
            raise ValueError(f"issue status must be one of: {', '.join(ISSUE_STATES)}")
    elif status:
        raise ValueError("status applies only to issues.")
    note_tags = sorted(set((tags or []) + [f"kind/{kind}"] + ([f"status/{status}"] if status else [])))
    header = f"**Kind:** {kind}" + (f"  \n**Status:** {status}" if status else "")
    note = save_note(
        title=title.strip(), body=f"{header}\n\n{body.strip()}\n", folder=f"Projects/{project}/{KIND_FOLDERS[kind]}",
        project=project, tags=note_tags, source_type="cie_knowledge", confidence="stated",
        replace_tags=_replace_tags,
    )
    return {**note, "kind": kind, "status": status or None}


def _read_note_meta(rel_path: str) -> tuple[dict[str, Any], str]:
    from .config import get_vault_root
    from .vault import _parse_frontmatter

    raw = (get_vault_root() / rel_path).read_text(encoding="utf-8", errors="replace")
    return _parse_frontmatter(raw)


def set_issue_status(project: str, title: str, status: str, resolution: str = "") -> dict[str, Any]:
    if status not in ISSUE_STATES:
        raise ValueError(f"status must be one of: {', '.join(ISSUE_STATES)}")
    from .vault import _safe_slug

    rel = f"Projects/{project}/Issues/{_safe_slug(title)}.md"
    from .config import get_vault_root

    if not (get_vault_root() / rel).exists():
        raise ValueError(f"No issue titled '{title}' in project '{project}'.")
    meta, body = _read_note_meta(rel)
    tags = [t for t in (meta.get("tags") or []) if not str(t).startswith("status/")]
    lines = [ln for ln in body.splitlines() if not ln.startswith("**Status:**") and not ln.startswith("**Kind:**")]
    extra = f"\n\nResolution ({utc_now()[:10]}): {resolution.strip()}" if resolution.strip() else ""
    return record(project, "issue", meta.get("title") or title, "\n".join(lines).strip() + extra, status, tags,
                  _replace_tags=True)


def query(project: str, kind: str = "", text: str = "", status: str = "", limit: int = 20) -> dict[str, Any]:
    from .vault import search_notes, sync_vault_index

    sync_vault_index()
    rows = get_storage().query(
        "SELECT rel_path, title, tags, updated_at FROM notes_index WHERE project = ? AND rel_path LIKE ? ORDER BY updated_at DESC",
        (project, f"Projects/{project}/%"),
    )
    if text:
        hits = {h["rel_path"] for h in search_notes(text, project=project, limit=200)}
        rows = [r for r in rows if r["rel_path"] in hits]
    out = []
    for r in rows:
        tags = str(r.get("tags") or "")
        if kind and f"kind/{kind}" not in tags:
            continue
        if status and f"status/{status}" not in tags:
            continue
        _, body = _read_note_meta(r["rel_path"])
        snippet = " ".join(ln for ln in body.splitlines() if ln and not ln.startswith("**"))[:240]
        found_kind = next((k for k in KINDS if f"kind/{k}" in tags), "")
        found_status = next((s for s in ISSUE_STATES if f"status/{s}" in tags), "")
        out.append({"title": r["title"], "kind": found_kind, "status": found_status or None,
                    "updated": r["updated_at"], "rel_path": r["rel_path"], "summary": snippet})
    return {"project": project, "count": len(out), "notes": out[: max(1, min(int(limit), 200))]}


def project_summary(project_id: str) -> dict[str, Any]:
    """One compact picture of a project: code, assets, open issues, recent
    decisions, and snapshots. Designed to replace many separate tool calls."""
    from .efficiency import incremental_index

    roots = [r for r in project_roots() if r["id"] == project_id]
    if not roots:
        raise ValueError(f"No registered project with id '{project_id}'.")
    refresh = incremental_index(project_id)
    storage = get_storage()
    langs = storage.query(
        "SELECT language, COUNT(*) AS c FROM files_index WHERE project_id = ? GROUP BY language ORDER BY c DESC LIMIT 8", (project_id,))
    sym = storage.query("SELECT COUNT(*) AS c FROM symbols_index WHERE project_id = ?", (project_id,))[0]["c"]
    deps = storage.query(
        "SELECT target, COUNT(*) AS c FROM dependencies_index WHERE project_id = ? GROUP BY target ORDER BY c DESC LIMIT 10", (project_id,))
    assets = storage.query("SELECT kind, COUNT(*) AS c FROM asset_meta WHERE project_id = ? GROUP BY kind", (project_id,))
    npot = 0
    for r in storage.query("SELECT meta_json FROM asset_meta WHERE project_id = ? AND kind = 'image'", (project_id,)):
        if json.loads(r["meta_json"]).get("power_of_two") is False:
            npot += 1
    snaps = storage.query(
        "SELECT snapshot_id, name, kind, created_at FROM snapshots WHERE project_id = ? ORDER BY created_at DESC LIMIT 3", (project_id,))
    issues = query(project_id, kind="issue", status="open", limit=10)["notes"]
    decisions = query(project_id, kind="decision", limit=5)["notes"]
    arch = query(project_id, kind="architecture", limit=3)["notes"]
    return {
        "project_id": project_id,
        "path": roots[0]["path"],
        "project_types": roots[0].get("project_types") or [],
        "files_by_language": {r["language"]: r["c"] for r in langs},
        "symbols": sym,
        "top_dependencies": [d["target"] for d in deps],
        "assets_by_kind": {a["kind"]: a["c"] for a in assets},
        "images_not_power_of_two": npot,
        "open_issues": [{"title": i["title"], "summary": i["summary"][:120]} for i in issues],
        "recent_decisions": [{"title": d["title"], "summary": d["summary"][:120]} for d in decisions],
        "architecture_notes": [a["title"] for a in arch],
        "recent_snapshots": snaps,
        "index_refresh": {k: refresh.get(k) for k in ("indexed", "removed")},
    }


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def _wrap(fn: Any, summary: str) -> Any:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"status": "ok", "summary": summary, "data": fn(args)}
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            return {"status": "error", "summary": str(exc)}
    return handler


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="knowledge.summary",
        area="knowledge",
        summary="One compact project overview: languages, symbols, top dependencies, assets by kind, open issues, recent decisions, snapshots. Start here.",
        input_schema={"project_id": "str"},
        permission="read",
        handler=_wrap(lambda a: project_summary(a["project_id"]), "Project summary ready."),
        tags=["knowledge", "overview", "context", "start"],
    ))
    reg.register(CapabilitySpec(
        id="knowledge.assets",
        area="knowledge",
        summary="List project assets with metadata (image size/alpha/power-of-two, audio length/rate, model vertices/triangles). Filter by kind or fields.",
        input_schema={"project_id": "str", "kind": "optional str", "where": "optional object", "limit": "optional int"},
        permission="read",
        handler=_wrap(lambda a: asset_query(a["project_id"], a.get("kind", ""), a.get("where"), int(a.get("limit", 50))), "Assets listed."),
        tags=["knowledge", "assets", "art", "audio", "3d"],
    ))
    # Notes are user-visible vault content (like memory.save), so they are
    # writes: approval-gated under 'assist'. They never touch project files.
    reg.register(CapabilitySpec(
        id="knowledge.record",
        area="knowledge",
        summary="Record a project decision, known issue, architecture note, history entry, preference, or reusable snippet in CIE's vault.",
        input_schema={"project": "str", "kind": "str", "title": "str", "body": "optional str", "status": "optional str", "tags": "optional list[str]"},
        permission="write",
        handler=_wrap(lambda a: record(a["project"], a["kind"], a["title"], a.get("body", ""), a.get("status", ""), a.get("tags")), "Knowledge recorded."),
        tags=["knowledge", "memory", "decision", "issue"],
    ))
    reg.register(CapabilitySpec(
        id="knowledge.issue_status",
        area="knowledge",
        summary="Mark a recorded issue open, fixed, or wontfix, with an optional resolution note.",
        input_schema={"project": "str", "title": "str", "status": "str", "resolution": "optional str"},
        permission="write",
        handler=_wrap(lambda a: set_issue_status(a["project"], a["title"], a["status"], a.get("resolution", "")), "Issue updated."),
        tags=["knowledge", "issue"],
    ))
    reg.register(CapabilitySpec(
        id="knowledge.query",
        area="knowledge",
        summary="Find recorded project knowledge by kind, issue status, and keywords.",
        input_schema={"project": "str", "kind": "optional str", "text": "optional str", "status": "optional str", "limit": "optional int"},
        permission="read",
        handler=_wrap(lambda a: query(a["project"], a.get("kind", ""), a.get("text", ""), a.get("status", ""), int(a.get("limit", 20))), "Knowledge found."),
        tags=["knowledge", "search", "memory"],
    ))
    reg.register(CapabilitySpec(
        id="knowledge.indexer_status",
        area="knowledge",
        summary="Show whether the background re-indexer is running, its interval, and its last pass.",
        input_schema={},
        permission="read",
        handler=_wrap(lambda a: indexer_status(), "Indexer status."),
        tags=["knowledge", "index"],
    ))
    reg.register(CapabilitySpec(
        id="knowledge.reindex_now",
        area="knowledge",
        summary="Run one incremental index pass over every registered project now.",
        input_schema={},
        permission="read",
        handler=_wrap(lambda a: run_index_pass(), "Index pass complete."),
        tags=["knowledge", "index"],
    ))
