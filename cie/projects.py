"""Project registration: register, list, update, and remove project folders
through CIE capabilities, with automatic project-type detection.

Removing a project only edits CIE's config and drops CIE's own regenerable
index rows. It never touches the project's files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_EXCLUDES,
    is_unc_or_network_path,
    load_config,
    project_roots,
    save_config,
    utc_now,
)

# Marker files/folders → project type. Checked in order; several can match.
_MARKERS: list[tuple[str, str]] = [
    ("project.godot", "godot"),
    ("default.project.json", "roblox-rojo"),
    ("aftman.toml", "roblox-tooling"),
    ("rokit.toml", "roblox-tooling"),
    ("wally.toml", "roblox-tooling"),
    ("fabric.mod.json", "minecraft-fabric-mod"),
    ("src/main/resources/fabric.mod.json", "minecraft-fabric-mod"),
    ("src/main/resources/META-INF/mods.toml", "minecraft-forge-mod"),
    ("src/main/resources/META-INF/neoforge.mods.toml", "minecraft-neoforge-mod"),
    ("pack.mcmeta", "minecraft-pack"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("package.json", "node"),
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("build.gradle", "gradle"),
    ("build.gradle.kts", "gradle"),
    ("index.html", "web"),
]
_GLOB_MARKERS: list[tuple[str, str]] = [
    ("*.uproject", "unreal"),
    ("*.rbxlx", "roblox-place"),
    ("*.rbxl", "roblox-place"),
    ("*.csproj", "dotnet"),
    ("*.sln", "dotnet"),
    ("*.blend", "blender"),
]

# Extra default excludes per type: build output and engine caches.
_TYPE_EXCLUDES: dict[str, list[str]] = {
    "godot": [".godot", ".import"],
    "unity": ["Library", "Temp", "Obj", "Logs", "UserSettings", "Build", "Builds"],
    "unreal": ["Binaries", "Intermediate", "Saved", "DerivedDataCache"],
    "node": ["node_modules", "dist", "build", ".next"],
    "python": ["dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"],
    "rust": ["target"],
    "gradle": ["build", ".gradle", "run"],
    "minecraft-fabric-mod": ["build", ".gradle", "run"],
    "minecraft-forge-mod": ["build", ".gradle", "run"],
    "minecraft-neoforge-mod": ["build", ".gradle", "run"],
    "dotnet": ["bin", "obj"],
}


def detect_project_types(path: Path) -> list[str]:
    """Return every project type whose marker exists in ``path`` (top level)."""
    found: list[str] = []
    for marker, kind in _MARKERS:
        if (path / marker).exists() and kind not in found:
            found.append(kind)
    for pattern, kind in _GLOB_MARKERS:
        if kind not in found and any(path.glob(pattern)):
            found.append(kind)
    if (path / "Assets").is_dir() and (path / "ProjectSettings").is_dir() and "unity" not in found:
        found.append("unity")
    return found or ["generic"]


def suggested_excludes(types: list[str]) -> list[str]:
    out: list[str] = []
    for kind in types:
        for item in _TYPE_EXCLUDES.get(kind, []):
            if item not in out and item not in DEFAULT_EXCLUDES:
                out.append(item)
    return out


def _validate_id(project_id: str) -> str:
    pid = project_id.strip()
    if not pid:
        raise ValueError("Project id cannot be empty.")
    if len(pid) > 64 or any(c in pid for c in '\\/:*?"<>|'):
        raise ValueError("Project id must be at most 64 characters with no path characters.")
    return pid


def _resolve_dir(path_str: str) -> Path:
    if is_unc_or_network_path(path_str):
        raise ValueError("UNC/network paths are blocked by security policy.")
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        raise ValueError(f"Project path must be absolute: {path_str}")
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"Project path must be an existing directory: {path_str}")
    if p == Path(p.anchor):
        raise ValueError("A drive root cannot be registered as a project.")
    return p


def register_project(
    path: str,
    project_id: str = "",
    excludes: list[str] | None = None,
    auto_excludes: bool = True,
) -> dict[str, Any]:
    p = _resolve_dir(path)
    pid = _validate_id(project_id or p.name)
    cfg = load_config()
    roots = project_roots(cfg)
    for r in roots:
        if Path(r["path"]).resolve() == p:
            return {"created": False, "project": r, "note": "Folder already registered."}
        if r.get("id") == pid:
            raise ValueError(f"Project id '{pid}' is already used by {r['path']}.")
        rp = Path(r["path"]).resolve()
        if p in rp.parents or rp in p.parents:
            raise ValueError(f"Folder overlaps registered project '{r['id']}' ({r['path']}).")
    types = detect_project_types(p)
    merged = list(excludes or [])
    if auto_excludes:
        for item in suggested_excludes(types):
            if item not in merged:
                merged.append(item)
    entry = {
        "id": pid,
        "path": str(p),
        "excludes": merged,
        "project_types": types,
        "added_at": utc_now(),
    }
    roots.append(entry)
    cfg["projects"] = roots
    save_config(cfg)
    return {"created": True, "project": entry}


def update_project(
    project_id: str,
    new_id: str = "",
    add_excludes: list[str] | None = None,
    remove_excludes: list[str] | None = None,
    redetect: bool = False,
) -> dict[str, Any]:
    cfg = load_config()
    roots = project_roots(cfg)
    target = next((r for r in roots if r.get("id") == project_id), None)
    if target is None:
        raise ValueError(f"No registered project with id '{project_id}'.")
    if new_id:
        nid = _validate_id(new_id)
        if any(r.get("id") == nid for r in roots if r is not target):
            raise ValueError(f"Project id '{nid}' is already in use.")
        target["id"] = nid
    ex = list(target.get("excludes") or [])
    for item in add_excludes or []:
        if item not in ex:
            ex.append(item)
    ex = [e for e in ex if e not in set(remove_excludes or [])]
    target["excludes"] = ex
    if redetect:
        target["project_types"] = detect_project_types(Path(target["path"]))
    target["updated_at"] = utc_now()
    cfg["projects"] = roots
    save_config(cfg)
    if new_id and new_id != project_id:
        _purge_index(project_id)
    return {"project": target, "reindex_needed": bool(new_id or add_excludes or remove_excludes)}


def _purge_index(project_id: str) -> int:
    from .storage import get_storage

    storage = get_storage()
    removed = {"n": 0}

    def _tx(conn: Any) -> None:
        for table, col in (
            ("files_fts", "project_id"),
            ("symbols_index", "project_id"),
            ("dependencies_index", "project_id"),
            ("index_meta", "project_id"),
            ("files_index", "project_id"),
        ):
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (project_id,))  # noqa: S608 - fixed table names
            removed["n"] += max(cur.rowcount, 0)

    storage.run_write(_tx)
    return removed["n"]


def remove_project(project_id: str, purge_index: bool = True) -> dict[str, Any]:
    cfg = load_config()
    roots = project_roots(cfg)
    kept = [r for r in roots if r.get("id") != project_id]
    if len(kept) == len(roots):
        raise ValueError(f"No registered project with id '{project_id}'.")
    cfg["projects"] = kept
    save_config(cfg)
    purged = _purge_index(project_id) if purge_index else 0
    return {"removed": project_id, "index_rows_purged": purged, "project_files_touched": False}


def list_projects() -> dict[str, Any]:
    from .storage import get_storage

    storage = get_storage()
    out = []
    for r in project_roots():
        stats = storage.query(
            "SELECT COUNT(*) AS files, MAX(indexed_at) AS last_indexed_at FROM files_index WHERE project_id = ?",
            (r["id"],),
        )[0]
        out.append({
            "id": r["id"],
            "path": r["path"],
            "exists": Path(r["path"]).is_dir(),
            "project_types": r.get("project_types") or detect_project_types(Path(r["path"])) if Path(r["path"]).is_dir() else [],
            "excludes": r.get("excludes") or [],
            "files_indexed": stats["files"],
            "last_indexed_at": stats["last_indexed_at"],
        })
    return {"projects": out, "count": len(out)}


def detect_project(path: str) -> dict[str, Any]:
    p = _resolve_dir(path)
    types = detect_project_types(p)
    return {"path": str(p), "project_types": types, "suggested_excludes": suggested_excludes(types)}


def _wrap(fn: Any, summary: str) -> Any:
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"status": "ok", "summary": summary, "data": fn(args)}
        except ValueError as exc:
            return {"status": "error", "summary": str(exc)}
    return handler


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="project.list",
        area="project",
        summary="List registered projects with type, excludes, and index status.",
        input_schema={},
        permission="read",
        handler=_wrap(lambda a: list_projects(), "Projects listed."),
        tags=["project", "setup"],
    ))
    reg.register(CapabilitySpec(
        id="project.detect",
        area="project",
        summary="Detect a folder's project type (Godot, Unity, Roblox, Minecraft mod, Python, ...) without registering it.",
        input_schema={"path": "str"},
        permission="read",
        handler=_wrap(lambda a: detect_project(a["path"]), "Project type detected."),
        tags=["project", "setup"],
    ))
    reg.register(CapabilitySpec(
        id="project.register",
        area="project",
        summary="Register a project folder (auto-detects type and build-folder excludes). Edits CIE config only.",
        input_schema={"path": "str", "project_id": "optional str", "excludes": "optional list[str]", "auto_excludes": "optional bool"},
        permission="write",
        handler=_wrap(
            lambda a: register_project(a["path"], a.get("project_id", ""), a.get("excludes"), a.get("auto_excludes", True)),
            "Project registered.",
        ),
        tags=["project", "setup"],
    ))
    reg.register(CapabilitySpec(
        id="project.update",
        area="project",
        summary="Rename a project or change its excludes. Edits CIE config only.",
        input_schema={
            "project_id": "str",
            "new_id": "optional str",
            "add_excludes": "optional list[str]",
            "remove_excludes": "optional list[str]",
            "redetect": "optional bool",
        },
        permission="write",
        handler=_wrap(
            lambda a: update_project(a["project_id"], a.get("new_id", ""), a.get("add_excludes"), a.get("remove_excludes"), a.get("redetect", False)),
            "Project updated.",
        ),
        tags=["project", "setup"],
    ))
    reg.register(CapabilitySpec(
        id="project.remove",
        area="project",
        summary="Unregister a project and drop CIE's regenerable index rows for it. Never touches project files.",
        input_schema={"project_id": "str", "purge_index": "optional bool"},
        permission="write",
        handler=_wrap(lambda a: remove_project(a["project_id"], a.get("purge_index", True)), "Project removed."),
        tags=["project", "setup"],
    ))
