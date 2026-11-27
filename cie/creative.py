"""Second wave of external-tool wrappers (v1.2.1).

Coding:  gdlint/gdformat, coverage, py-spy profiling, Docker sandbox, read-only GitHub (gh).
3D/art:  Blender export + render, trimesh mesh check/repair/simplify, glTF validation, SVG export.
Sound:   FluidSynth MIDI render, Pure Data and SuperCollider headless runs.

Same rules as cie/external.py: allowlisted executables, shell=False, paths confined to approved
projects, capped output, and "tool_missing" with an install hint instead of a crash.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .config import get_cie_home, load_config, project_roots, resolve_in_projects
from .diagnostics import detect_blender
from .external import (
    ALLOWED_TOOLS,
    INSTALL_HINTS,
    ToolMissing,
    _missing,
    _run,
    _safe_out,
    find_tool,
)

NEW_TOOLS = {
    "gdlint": "pip install gdtoolkit (GDScript linter for Godot 4).",
    "gdformat": "pip install gdtoolkit (GDScript formatter for Godot 4).",
    "coverage": "pip install coverage",
    "py-spy": "pip install py-spy",
    "docker": "Install Docker Desktop, start it, then run: docker pull python:3.12-slim",
    "gh": "Install the GitHub CLI from cli.github.com, then run: gh auth login",
    "gltf_validator": "Download the Khronos glTF-Validator release from github.com/KhronosGroup/glTF-Validator and put it on PATH.",
    "inkscape": "Install Inkscape 1.x and add it to PATH (or pip install cairosvg for the built-in fallback).",
    "fluidsynth": "Install FluidSynth (fluidsynth.org) and add it to PATH. You also need a .sf2 soundfont in your project.",
    "pd": "Install Pure Data (puredata.info) and add pd.com / pd to PATH.",
    "sclang": "Install SuperCollider (supercollider.github.io) and add sclang to PATH.",
}
INSTALL_HINTS.update(NEW_TOOLS)
ALLOWED_TOOLS.update(NEW_TOOLS)

def _tmpdir() -> Path:
    base = get_cie_home() / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="cie_", dir=str(base)))


def _root_of(entry: dict[str, Any]) -> Path:
    return Path(entry["path"]).resolve()


# ======================= Coding =======================

_GDLINT_LINE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): (?P<kind>Error|Warning): (?P<msg>.*?)(?: \((?P<rule>[\w-]+)\))?$")


def lint_gdlint(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("gdlint")
    except ToolMissing:
        return _missing("gdlint", "lint.gdlint")
    target, _ = resolve_in_projects(path or ".")
    res = _run([exe, str(target)], timeout=180)
    findings = []
    for ln in (res["stdout"] + "\n" + res["stderr"]).splitlines():
        m = _GDLINT_LINE.match(ln.strip())
        if m:
            findings.append({"file": m["file"], "line": int(m["line"]), "kind": m["kind"], "rule": m["rule"], "message": m["msg"]})
    return {"status": "ok", "summary": f"gdlint: {len(findings)} finding(s)", "data": {"findings": findings[:200], "total": len(findings)}}


def lint_gdformat(path: str = "", apply: bool = False) -> dict[str, Any]:
    """Check GDScript formatting (default) or rewrite files in place when apply=True."""
    try:
        exe = find_tool("gdformat")
    except ToolMissing:
        return _missing("gdformat", "lint.gdformat")
    target, _ = resolve_in_projects(path or ".")
    argv = [exe, str(target)] if apply else [exe, "--check", "--diff", str(target)]
    res = _run(argv, timeout=180)
    text = res["stdout"] + res["stderr"]
    return {
        "status": "ok",
        "summary": ("Formatted files." if apply else ("Formatting is clean." if res["returncode"] == 0 else "Files need formatting.")),
        "data": {"returncode": res["returncode"], "output": text[-8000:]},
    }


def coding_coverage(command_key: str = "unittest", project_id: str = "", max_files: int = 40) -> dict[str, Any]:
    """Run an allowlisted 'python -m <runner>' test command under coverage and report missed lines."""
    cfg = load_config()
    cmd = list((cfg.get("allowlisted_commands") or {}).get(command_key) or [])
    if len(cmd) < 3 or cmd[0] not in {"python", "python3", "py"} or cmd[1] != "-m":
        raise PermissionError(f"'{command_key}' must be an allowlisted 'python -m <runner>' command.")
    try:
        exe = find_tool("coverage")
    except ToolMissing:
        return _missing("coverage", "coding.coverage")
    
    roots = [r for r in project_roots(cfg) if not project_id or r["id"] == project_id]
    if not roots:
        raise PermissionError("No approved project folder to run coverage in.")
    root = _root_of(roots[0])
    work = _tmpdir()
    data_file = work / ".coverage"
    run = _run([exe, "run", f"--data-file={data_file}", "--source", str(root), "-m", *cmd[2:]], timeout=300, cwd=root)
    rep = _run([exe, "json", f"--data-file={data_file}", "-o", str(work / "cov.json"), "-q"], timeout=120, cwd=root)
    if rep["returncode"] != 0 or not (work / "cov.json").exists():
        return {"status": "error", "summary": "Coverage produced no data.", "data": {"run_tail": (run["stdout"] + run["stderr"])[-3000:]}}
    report = json.loads((work / "cov.json").read_text(encoding="utf-8"))
    files = []
    for name, info in report.get("files", {}).items():
        s = info.get("summary", {})
        files.append({"file": name, "percent": round(s.get("percent_covered", 0.0), 1),
                      "missing_lines": info.get("missing_lines", [])[:60]})
    files.sort(key=lambda f: f["percent"])
    total = round(report.get("totals", {}).get("percent_covered", 0.0), 1)
    shutil.rmtree(work, ignore_errors=True)
    return {"status": "ok", "summary": f"Total coverage {total}% (tests exit code {run['returncode']}).",
            "data": {"total_percent": total, "tests_passed": run["returncode"] == 0, "least_covered": files[: max(1, min(int(max_files), 200))]}}


def coding_profile(script: str, duration_sec: int = 20, rate: int = 100, top: int = 25) -> dict[str, Any]:
    """Profile a project Python script with py-spy and return the hottest functions."""
    try:
        exe = find_tool("py-spy")
    except ToolMissing:
        return _missing("py-spy", "coding.profile")
    src, entry = resolve_in_projects(script)
    if src.suffix.lower() != ".py" or not src.is_file():
        raise ValueError("script must be a .py file inside a project.")
    dur = max(1, min(int(duration_sec), 120))
    hz = max(10, min(int(rate), 1000))
    work = _tmpdir()
    out = work / "profile.txt"
    python = shutil.which("python") or shutil.which("python3") or "python"
    res = _run([exe, "record", "-r", str(hz), "-d", str(dur), "-f", "raw", "-o", str(out), "--", python, str(src)],
               timeout=dur + 60, cwd=_root_of(entry))
    if not out.exists():
        return {"status": "error", "summary": "py-spy wrote no profile (on some systems it needs admin rights).",
                "data": {"stderr_tail": res["stderr"][-2000:]}}
    self_time: Counter[str] = Counter()
    total_time: Counter[str] = Counter()
    samples = 0
    for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
        stack, _, count = line.rpartition(" ")
        if not count.isdigit():
            continue
        n = int(count)
        samples += n
        frames = stack.split(";")
        self_time[frames[-1]] += n
        for fr in set(frames):
            total_time[fr] += n
    shutil.rmtree(work, ignore_errors=True)
    k = max(1, min(int(top), 100))
    def pct(n: int) -> float:
        return round(100.0 * n / samples, 1) if samples else 0.0

    return {"status": "ok", "summary": f"{samples} samples; hottest: {self_time.most_common(1)[0][0] if samples else 'n/a'}",
            "data": {"samples": samples,
                     "self": [{"frame": f, "percent": pct(n)} for f, n in self_time.most_common(k)],
                     "cumulative": [{"frame": f, "percent": pct(n)} for f, n in total_time.most_common(k)]}}


_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,100}(:[A-Za-z0-9._-]{1,60})?$")


def sandbox_docker_argv(docker: str, root: Path, rel_script: str, image: str, timeout: int) -> list[str]:
    """Build the locked-down docker command (separate so it can be unit-tested without a daemon)."""
    return [
        docker, "run", "--rm", "--pull", "never",
        "--network", "none", "--memory", "1g", "--cpus", "2", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=256m", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{root}:/work:ro", "-w", "/work",
        image, "timeout", str(timeout), "python", rel_script,
    ]


def sandbox_run(script: str, image: str = "", timeout_sec: int = 60) -> dict[str, Any]:
    """Run a project Python script inside an isolated, offline, read-only Docker container."""
    try:
        docker = find_tool("docker")
    except ToolMissing:
        return _missing("docker", "sandbox.run")
    img = (image or load_config().get("sandbox_image") or "python:3.12-slim").strip()
    if not _IMAGE_RE.match(img):
        raise ValueError("Invalid image name.")
    src, entry = resolve_in_projects(script)
    if src.suffix.lower() != ".py" or not src.is_file():
        raise ValueError("script must be a .py file inside a project.")
    root = _root_of(entry)
    t = max(1, min(int(timeout_sec), 600))
    rel = src.relative_to(root).as_posix()
    res = _run(sandbox_docker_argv(docker, root, rel, img, t), timeout=t + 30)
    if "No such image" in res["stderr"] or "pull access denied" in res["stderr"]:
        return {"status": "error", "summary": f"Image '{img}' is not downloaded. Run: docker pull {img}"}
    return {"status": "ok" if res["returncode"] == 0 else "error",
            "summary": f"Sandbox exit code {res['returncode']}.",
            "data": {"stdout": res["stdout"][-8000:], "stderr": res["stderr"][-4000:], "timed_out": res["timed_out"]}}


_REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_GH_STATES = {"open", "closed", "merged", "all"}


def _gh(args: list[str], repo: str, project_id: str) -> dict[str, Any]:
    try:
        gh = find_tool("gh")
    except ToolMissing:
        return _missing("gh", "github")
    argv = [gh, *args]
    cwd = None
    if repo:
        if not _REPO_RE.match(repo):
            raise ValueError("repo must look like owner/name.")
        argv += ["--repo", repo]
    else:
        roots = [r for r in project_roots() if not project_id or r["id"] == project_id]
        if not roots:
            raise PermissionError("Give repo='owner/name' or approve a project folder that is a git clone.")
        cwd = _root_of(roots[0])
    res = _run(argv, timeout=60, cwd=cwd)
    if res["returncode"] != 0:
        msg = res["stderr"].strip()[-600:] or "gh failed."
        if "auth login" in msg or "authentication" in msg.lower() or "Bad credentials" in msg:
            msg += " Run: gh auth login"
        return {"status": "error", "summary": msg}
    try:
        return {"status": "ok", "summary": "GitHub data.", "data": json.loads(res["stdout"] or "null")}
    except json.JSONDecodeError:
        return {"status": "ok", "summary": "GitHub output.", "data": {"text": res["stdout"][-20000:]}}


def github_list(kind: str = "pr", state: str = "open", limit: int = 20, repo: str = "", project_id: str = "") -> dict[str, Any]:
    if kind not in {"pr", "issue"}:
        raise ValueError("kind must be 'pr' or 'issue'.")
    if state not in _GH_STATES or (kind == "issue" and state == "merged"):
        raise ValueError("Invalid state.")
    fields = "number,title,state,author,updatedAt,labels"
    return _gh([kind, "list", "--state", state, "--limit", str(max(1, min(int(limit), 100))), "--json", fields], repo, project_id)


def github_view(kind: str, number: int, repo: str = "", project_id: str = "") -> dict[str, Any]:
    if kind not in {"pr", "issue"}:
        raise ValueError("kind must be 'pr' or 'issue'.")
    num = int(number)
    if num < 1:
        raise ValueError("number must be positive.")
    fields = "number,title,state,author,body,comments,labels,url" + (",files,reviewDecision" if kind == "pr" else "")
    return _gh([kind, "view", str(num), "--json", fields], repo, project_id)


def github_pr_diff(number: int, repo: str = "", project_id: str = "") -> dict[str, Any]:
    num = int(number)
    if num < 1:
        raise ValueError("number must be positive.")
    res = _gh(["pr", "diff", str(num), "--color", "never"], repo, project_id)
    if res.get("status") == "ok" and isinstance(res.get("data"), dict) and "text" in res["data"]:
        res["summary"] = f"Diff for PR #{num} ({len(res['data']['text'])} chars, capped)."
    return res


# ======================= 3D and art =======================

BLENDER_EXPORT_FORMATS = {".glb": "GLB", ".gltf": "GLTF_SEPARATE", ".fbx": "FBX", ".obj": "OBJ"}
BLENDER_ENGINES = {"": None, "cycles": "CYCLES", "workbench": "BLENDER_WORKBENCH"}


def _blender_or_missing(capability: str) -> str | dict[str, Any]:
    exe = detect_blender()
    if not exe:
        return {"status": "tool_missing", "capability": capability,
                "summary": "Blender was not found. Install Blender 4.x or 5.x, or set blender_path in the CIE config."}
    return exe


def _blender_run(exe: str, blend: Path, script: str, timeout: int) -> dict[str, Any]:
    # --disable-autoexec: never run Python embedded inside the .blend file.
    return _run([exe, "-b", "--disable-autoexec", "--factory-startup", str(blend), "--python-expr", script], timeout=timeout)


def blender_export(source: str, output: str) -> dict[str, Any]:
    exe = _blender_or_missing("art.blender_export")
    if isinstance(exe, dict):
        return exe
    src, _ = resolve_in_projects(source)
    if src.suffix.lower() != ".blend":
        raise ValueError("source must be a .blend file.")
    out = _safe_out(output)
    fmt = BLENDER_EXPORT_FORMATS.get(out.suffix.lower())
    if not fmt:
        raise ValueError("output must end in .glb, .gltf, .fbx, or .obj.")
    target = json.dumps(str(out))
    if fmt in {"GLB", "GLTF_SEPARATE"}:
        op = f"bpy.ops.export_scene.gltf(filepath={target}, export_format='{fmt}')"
    elif fmt == "FBX":
        op = f"bpy.ops.export_scene.fbx(filepath={target})"
    else:
        op = f"bpy.ops.wm.obj_export(filepath={target})"
    res = _blender_run(exe, src, f"import bpy\n{op}\n", timeout=300)
    ok = out.exists()
    return {"status": "ok" if ok else "error", "summary": f"Exported {out.name}." if ok else "Blender export failed.",
            "artifacts": [str(out)] if ok else [], "data": {"log_tail": (res["stdout"] + res["stderr"])[-3000:]}}


def blender_render(source: str, output: str, width: int = 1280, height: int = 720, frame: int = 1,
                   engine: str = "", samples: int = 64) -> dict[str, Any]:
    exe = _blender_or_missing("art.blender_render")
    if isinstance(exe, dict):
        return exe
    src, _ = resolve_in_projects(source)
    if src.suffix.lower() != ".blend":
        raise ValueError("source must be a .blend file.")
    out = _safe_out(output)
    if out.suffix.lower() != ".png":
        raise ValueError("output must be a .png file.")
    if engine not in BLENDER_ENGINES:
        raise ValueError("engine must be '', 'cycles', or 'workbench' ('' keeps the scene's engine).")
    w, h = max(16, min(int(width), 7680)), max(16, min(int(height), 4320))
    eng = BLENDER_ENGINES[engine]
    lines = [
        "import bpy",
        "s = bpy.context.scene",
        f"s.render.resolution_x = {w}",
        f"s.render.resolution_y = {h}",
        "s.render.resolution_percentage = 100",
        "s.render.image_settings.file_format = 'PNG'",
        f"s.frame_set({int(frame)})",
        f"s.render.filepath = {json.dumps(str(out))}",
    ]
    if eng:
        lines.append(f"s.render.engine = '{eng}'")
    if eng == "CYCLES":
        lines.append(f"s.cycles.samples = {max(1, min(int(samples), 4096))}")
    lines.append("bpy.ops.render.render(write_still=True)")
    res = _blender_run(exe, src, "\n".join(lines) + "\n", timeout=900)
    ok = out.exists()
    return {"status": "ok" if ok else "error", "summary": f"Rendered {w}x{h} to {out.name}." if ok else "Blender render failed.",
            "artifacts": [str(out)] if ok else [], "data": {"log_tail": (res["stdout"] + res["stderr"])[-3000:]}}


MESH_EXTS = {".obj", ".stl", ".ply", ".glb", ".gltf", ".off"}


def _load_mesh(path: Path):
    import trimesh

    loaded = trimesh.load(str(path), force="mesh", process=False)
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ValueError("No triangle mesh found in that file.")
    return loaded


def mesh_check(path: str) -> dict[str, Any]:
    """Report mesh health: holes, non-manifold edges, flipped faces, degenerate/duplicate faces, size."""
    import numpy as np
    import trimesh

    src, _ = resolve_in_projects(path)
    if src.suffix.lower() not in MESH_EXTS:
        raise ValueError(f"Supported mesh formats: {', '.join(sorted(MESH_EXTS))}.")
    m = _load_mesh(src)
    edges = m.edges_sorted
    _, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = int(np.sum(counts == 1))
    non_manifold = int(np.sum(counts > 2))
    degenerate = int(np.sum(m.area_faces <= 1e-12))
    dup = int(len(m.faces) - len(np.unique(np.sort(m.faces, axis=1), axis=0)))
    merged = m.copy()
    merged.merge_vertices()
    problems = []
    if boundary:
        problems.append(f"{boundary} open edge(s): the mesh has holes")
    if non_manifold:
        problems.append(f"{non_manifold} non-manifold edge(s)")
    if not m.is_winding_consistent:
        problems.append("inconsistent face winding (some normals flipped)")
    if degenerate:
        problems.append(f"{degenerate} zero-area face(s)")
    if dup:
        problems.append(f"{dup} duplicate face(s)")
    if len(merged.vertices) < len(m.vertices):
        problems.append(f"{len(m.vertices) - len(merged.vertices)} duplicate vertex(es)")
    ext = m.bounding_box.extents.tolist()
    return {"status": "ok",
            "summary": "Mesh looks clean." if not problems else "; ".join(problems),
            "data": {"vertices": len(m.vertices), "faces": len(m.faces),
                     "watertight": bool(m.is_watertight), "winding_consistent": bool(m.is_winding_consistent),
                     "open_edges": boundary, "non_manifold_edges": non_manifold,
                     "degenerate_faces": degenerate, "duplicate_faces": dup,
                     "bounds_size": [round(v, 4) for v in ext],
                     "volume": round(float(m.volume), 6) if m.is_watertight else None,
                     "trimesh_version": trimesh.__version__,
                     "problems": problems}}


def mesh_repair(source: str, output: str) -> dict[str, Any]:
    """Merge vertices, drop degenerate/duplicate faces, fix winding and normals, fill small holes; write a new file."""
    import trimesh

    src, _ = resolve_in_projects(source)
    out = _safe_out(output)
    if out.suffix.lower() not in MESH_EXTS - {".gltf"}:
        raise ValueError("output must be .obj, .stl, .ply, .glb, or .off.")
    if out == src:
        raise ValueError("output must be a different file from source.")
    before = mesh_check(source)["data"]
    m = _load_mesh(src)
    m.merge_vertices()
    m.update_faces(m.nondegenerate_faces())
    m.update_faces(m.unique_faces())
    m.remove_unreferenced_vertices()
    trimesh.repair.fix_winding(m)
    trimesh.repair.fix_inversion(m)
    trimesh.repair.fix_normals(m)
    trimesh.repair.fill_holes(m)
    m.export(str(out))
    after = mesh_check(str(out))["data"]
    return {"status": "ok", "summary": f"Repaired mesh written to {out.name}.", "artifacts": [str(out)],
            "data": {"before": {k: before[k] for k in ("faces", "watertight", "open_edges", "problems")},
                     "after": {k: after[k] for k in ("faces", "watertight", "open_edges", "problems")}}}


def mesh_simplify(source: str, output: str, target_faces: int = 0, ratio: float = 0.5) -> dict[str, Any]:
    """Reduce polygon count with quadric decimation (for LODs). Give target_faces, or ratio of faces to keep."""
    src, _ = resolve_in_projects(source)
    out = _safe_out(output)
    if out.suffix.lower() not in MESH_EXTS - {".gltf"}:
        raise ValueError("output must be .obj, .stl, .ply, .glb, or .off.")
    if out == src:
        raise ValueError("output must be a different file from source.")
    m = _load_mesh(src)
    n0 = len(m.faces)
    target = int(target_faces) if int(target_faces) > 0 else int(n0 * max(0.01, min(float(ratio), 1.0)))
    target = max(4, min(target, n0))
    try:
        low = m.simplify_quadric_decimation(face_count=target)
    except (ImportError, ModuleNotFoundError):
        return {"status": "tool_missing", "summary": "Mesh simplification needs: pip install fast-simplification"}
    low.export(str(out))
    return {"status": "ok", "summary": f"{n0} -> {len(low.faces)} faces ({out.name}).", "artifacts": [str(out)],
            "data": {"faces_before": n0, "faces_after": len(low.faces)}}


def _gltf_basic_check(path: Path) -> list[str]:
    """Lightweight structural checks used when the Khronos validator is not installed."""
    raw = path.read_bytes()
    issues: list[str] = []
    bin_len = 0
    if path.suffix.lower() == ".glb":
        if len(raw) < 20 or raw[:4] != b"glTF":
            return ["Not a GLB file (bad magic)."]
        _, version, length = struct.unpack_from("<4sII", raw, 0)
        if version != 2:
            issues.append(f"GLB version {version}, expected 2.")
        if length != len(raw):
            issues.append(f"GLB header length {length} != file size {len(raw)}.")
        jlen, jtype = struct.unpack_from("<II", raw, 12)
        if jtype != 0x4E4F534A:
            return [*issues, "First GLB chunk is not JSON."]
        doc = json.loads(raw[20:20 + jlen].decode("utf-8"))
        off = 20 + jlen
        if off + 8 <= len(raw):
            bin_len, btype = struct.unpack_from("<II", raw, off)
            if btype != 0x004E4942:
                issues.append("Second GLB chunk is not BIN.")
    else:
        doc = json.loads(raw.decode("utf-8"))
    if str((doc.get("asset") or {}).get("version")) != "2.0":
        issues.append("asset.version must be '2.0'.")
    buffers = doc.get("buffers", [])
    for i, b in enumerate(buffers):
        uri = b.get("uri")
        if uri and not uri.startswith("data:"):
            f = (path.parent / uri).resolve()
            if not f.exists():
                issues.append(f"buffers[{i}] file missing: {uri}")
            elif f.stat().st_size < int(b.get("byteLength", 0)):
                issues.append(f"buffers[{i}] file is smaller than byteLength.")
        elif not uri and path.suffix.lower() == ".glb" and int(b.get("byteLength", 0)) > bin_len:
            issues.append(f"buffers[{i}] byteLength exceeds GLB BIN chunk.")
    views = doc.get("bufferViews", [])
    for i, v in enumerate(views):
        bi = v.get("buffer", -1)
        if not 0 <= bi < len(buffers):
            issues.append(f"bufferViews[{i}] points to missing buffer {bi}.")
        elif v.get("byteOffset", 0) + v.get("byteLength", 0) > int(buffers[bi].get("byteLength", 0)):
            issues.append(f"bufferViews[{i}] runs past the end of buffer {bi}.")
    for i, a in enumerate(doc.get("accessors", [])):
        bv = a.get("bufferView")
        if bv is not None and not 0 <= bv < len(views):
            issues.append(f"accessors[{i}] points to missing bufferView {bv}.")
    n_acc = len(doc.get("accessors", []))
    for mi, mesh in enumerate(doc.get("meshes", [])):
        for pi, prim in enumerate(mesh.get("primitives", [])):
            if "POSITION" not in (prim.get("attributes") or {}):
                issues.append(f"meshes[{mi}].primitives[{pi}] has no POSITION.")
            for name, idx in (prim.get("attributes") or {}).items():
                if not 0 <= idx < n_acc:
                    issues.append(f"meshes[{mi}].primitives[{pi}].{name} points to missing accessor {idx}.")
    for i, img in enumerate(doc.get("images", [])):
        uri = img.get("uri")
        if uri and not uri.startswith("data:") and not (path.parent / uri).exists():
            issues.append(f"images[{i}] file missing: {uri}")
    return issues


def gltf_validate(path: str) -> dict[str, Any]:
    """Validate a .gltf/.glb. Uses the Khronos validator if installed, otherwise built-in structural checks."""
    src, _ = resolve_in_projects(path)
    if src.suffix.lower() not in {".gltf", ".glb"}:
        raise ValueError("path must be a .gltf or .glb file.")
    exe = shutil.which("gltf_validator")
    if exe:
        res = _run([exe, "-o", str(src)], timeout=120)
        try:
            report = json.loads(res["stdout"])
            issues = report.get("issues", {})
            msgs = [{"code": m.get("code"), "severity": m.get("severity"), "pointer": m.get("pointer"), "message": m.get("message")}
                    for m in issues.get("messages", [])[:100]]
            return {"status": "ok", "summary": f"Khronos validator: {issues.get('numErrors', 0)} error(s), {issues.get('numWarnings', 0)} warning(s).",
                    "data": {"validator": "khronos", "messages": msgs}}
        except json.JSONDecodeError:
            pass
    issues = _gltf_basic_check(src)
    return {"status": "ok",
            "summary": ("Basic check passed" if not issues else f"{len(issues)} problem(s) found") + " (built-in checks; install gltf_validator for the full Khronos check).",
            "data": {"validator": "basic", "issues": issues}}


def svg_export(source: str, output: str, width: int = 0, height: int = 0) -> dict[str, Any]:
    """Export an SVG to PNG at an exact size. Uses Inkscape if installed, otherwise cairosvg."""
    src, _ = resolve_in_projects(source)
    if src.suffix.lower() != ".svg":
        raise ValueError("source must be an .svg file.")
    out = _safe_out(output)
    if out.suffix.lower() != ".png":
        raise ValueError("output must be a .png file.")
    w, h = int(width), int(height)
    if w < 0 or h < 0 or w > 16384 or h > 16384:
        raise ValueError("width/height must be between 0 and 16384 (0 = keep aspect).")
    exe = shutil.which("inkscape")
    if exe:
        argv = [exe, str(src), "--export-type=png", f"--export-filename={out}"]
        if w:
            argv.append(f"--export-width={w}")
        if h:
            argv.append(f"--export-height={h}")
        res = _run(argv, timeout=180)
        engine = "inkscape"
    else:
        try:
            import cairosvg
        except ImportError:
            return _missing("inkscape", "art.svg_export")
        # unsafe=False blocks external file and network references inside the SVG.
        cairosvg.svg2png(url=str(src), write_to=str(out), output_width=w or None, output_height=h or None, unsafe=False)
        res = {"stdout": "", "stderr": ""}
        engine = "cairosvg"
    ok = out.exists()
    size = None
    if ok:
        from PIL import Image
        with Image.open(out) as im:
            size = list(im.size)
    return {"status": "ok" if ok else "error", "summary": f"Exported {out.name} {size} with {engine}." if ok else "SVG export failed.",
            "artifacts": [str(out)] if ok else [], "data": {"engine": engine, "size": size, "log_tail": (res["stdout"] + res["stderr"])[-2000:]}}


# ======================= Sound =======================

def audio_midi_render(midi: str, soundfont: str, output: str, sample_rate: int = 44100, gain: float = 0.5) -> dict[str, Any]:
    """Render a MIDI file to WAV through a .sf2 soundfont with FluidSynth."""
    src, _ = resolve_in_projects(midi)
    sf2, _ = resolve_in_projects(soundfont)
    if src.suffix.lower() not in {".mid", ".midi"}:
        raise ValueError("midi must be a .mid or .midi file.")
    if sf2.suffix.lower() not in {".sf2", ".sf3"}:
        raise ValueError("soundfont must be a .sf2 or .sf3 file.")
    out = _safe_out(output)
    if out.suffix.lower() != ".wav":
        raise ValueError("output must be a .wav file.")
    rate = int(sample_rate)
    if rate not in {22050, 32000, 44100, 48000, 96000}:
        raise ValueError("sample_rate must be 22050, 32000, 44100, 48000, or 96000.")
    g = max(0.0, min(float(gain), 5.0))
    try:
        exe = find_tool("fluidsynth")
    except ToolMissing:
        return _missing("fluidsynth", "audio.midi_render")
    res = _run([exe, "-ni", "-q", "-g", f"{g:.2f}", "-r", str(rate), "-F", str(out), "-T", "wav", str(sf2), str(src)], timeout=300)
    ok = out.exists() and out.stat().st_size > 44
    return {"status": "ok" if ok else "error", "summary": f"Rendered {out.name}." if ok else "FluidSynth render failed.",
            "artifacts": [str(out)] if ok else [], "data": {"log_tail": (res["stdout"] + res["stderr"])[-2000:]}}


def _list_new_files(root: Path, since: float) -> list[str]:
    found = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".wav", ".aif", ".aiff", ".flac", ".ogg"} and p.stat().st_mtime >= since:
            found.append(str(p))
    return sorted(found)[:50]


def audio_puredata_run(patch: str, timeout_sec: int = 60) -> dict[str, Any]:
    """Run a Pure Data patch headless in batch mode. The patch writes its own audio (writesf~) and sends 'pd quit'."""
    import time

    src, entry = resolve_in_projects(patch)
    if src.suffix.lower() != ".pd":
        raise ValueError("patch must be a .pd file.")
    try:
        exe = find_tool("pd")
    except ToolMissing:
        return _missing("pd", "audio.puredata_run")
    t = max(1, min(int(timeout_sec), 600))
    start = time.time()
    res = _run([exe, "-nogui", "-batch", "-noprefs", "-path", str(src.parent), "-open", str(src)], timeout=t, cwd=src.parent)
    produced = _list_new_files(_root_of(entry), start)
    hint = " The patch must end itself with a [; pd quit( message." if res["timed_out"] else ""
    return {"status": "ok" if res["returncode"] == 0 else "error",
            "summary": f"Pure Data finished; {len(produced)} new audio file(s).{hint}",
            "artifacts": produced, "data": {"log_tail": (res["stdout"] + res["stderr"])[-3000:], "timed_out": res["timed_out"]}}


def audio_supercollider_run(script: str, timeout_sec: int = 120) -> dict[str, Any]:
    """Run a SuperCollider .scd script with sclang (e.g. a Score.recordNRT render). The script must call 0.exit."""
    import time

    src, entry = resolve_in_projects(script)
    if src.suffix.lower() != ".scd":
        raise ValueError("script must be a .scd file.")
    try:
        exe = find_tool("sclang")
    except ToolMissing:
        return _missing("sclang", "audio.supercollider_run")
    t = max(1, min(int(timeout_sec), 900))
    start = time.time()
    res = _run([exe, str(src)], timeout=t, cwd=src.parent)
    produced = _list_new_files(_root_of(entry), start)
    hint = " The script must finish with 0.exit;" if res["timed_out"] else ""
    return {"status": "ok" if res["returncode"] == 0 else "error",
            "summary": f"sclang finished; {len(produced)} new audio file(s).{hint}",
            "artifacts": produced, "data": {"log_tail": (res["stdout"] + res["stderr"])[-3000:], "timed_out": res["timed_out"]}}


# ======================= Registration =======================

def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def spec(cid, area, summary, perm, fn, schema, tags):
        reg.register(CapabilitySpec(id=cid, area=area, summary=summary, input_schema=schema, permission=perm,
                                    handler=lambda args, _fn=fn: _fn(**dict(args)), tags=tags))

    # Coding
    spec("lint.gdlint", "lint", "Lint GDScript (Godot) files with gdlint.", "read", lint_gdlint,
         {"path": "optional str"}, ["lint", "godot", "gdscript"])
    spec("lint.gdformat", "lint", "Check GDScript formatting, or rewrite files with apply=true.", "write", lint_gdformat,
         {"path": "optional str", "apply": "optional bool"}, ["format", "godot", "gdscript"])
    spec("coding.coverage", "coding", "Run allowlisted tests under coverage; list least-covered files and missed lines.", "exec", coding_coverage,
         {"command_key": "optional str", "project_id": "optional str", "max_files": "optional int"}, ["tests", "coverage"])
    spec("coding.profile", "coding", "Profile a project Python script with py-spy; hottest functions by %.", "exec", coding_profile,
         {"script": "str", "duration_sec": "optional int", "rate": "optional int", "top": "optional int"}, ["profile", "performance"])
    spec("sandbox.run", "sandbox", "Run a project script in an offline, read-only, resource-limited Docker container.", "exec", sandbox_run,
         {"script": "str", "image": "optional str", "timeout_sec": "optional int"}, ["sandbox", "docker", "isolation"])
    spec("github.list", "github", "List PRs or issues (read-only, via gh).", "read", github_list,
         {"kind": "pr|issue", "state": "optional str", "limit": "optional int", "repo": "optional owner/name", "project_id": "optional str"},
         ["github", "pr", "issues"])
    spec("github.view", "github", "Read one PR or issue with comments (read-only).", "read", github_view,
         {"kind": "pr|issue", "number": "int", "repo": "optional owner/name", "project_id": "optional str"}, ["github", "pr", "issues"])
    spec("github.pr_diff", "github", "Read a PR's diff (read-only).", "read", github_pr_diff,
         {"number": "int", "repo": "optional owner/name", "project_id": "optional str"}, ["github", "pr", "diff"])
    # 3D and art
    spec("art.blender_export", "art", "Export a .blend scene to .glb, .gltf, .fbx, or .obj (embedded scripts disabled).", "write", blender_export,
         {"source": "str", "output": "str"}, ["3d", "blender", "export"])
    spec("art.blender_render", "art", "Render a .blend frame to PNG at a set size (cycles, workbench, or scene engine).", "write", blender_render,
         {"source": "str", "output": "str", "width": "optional int", "height": "optional int", "frame": "optional int",
          "engine": "optional str", "samples": "optional int"}, ["3d", "blender", "render"])
    spec("mesh.check", "mesh", "Mesh health: holes, non-manifold edges, flipped faces, duplicates, size, volume.", "read", mesh_check,
         {"path": "str"}, ["3d", "mesh", "validate"])
    spec("mesh.repair", "mesh", "Write a repaired copy of a mesh (merge, dedupe, fix normals, fill holes).", "write", mesh_repair,
         {"source": "str", "output": "str"}, ["3d", "mesh", "repair"])
    spec("mesh.simplify", "mesh", "Write a lower-poly copy of a mesh (quadric decimation) for LODs.", "write", mesh_simplify,
         {"source": "str", "output": "str", "target_faces": "optional int", "ratio": "optional float"}, ["3d", "mesh", "lod"])
    spec("gltf.validate", "mesh", "Validate a .gltf/.glb (Khronos validator if installed, else built-in checks).", "read", gltf_validate,
         {"path": "str"}, ["3d", "gltf", "validate"])
    spec("art.svg_export", "art", "Export an SVG to PNG at an exact size (Inkscape, or cairosvg fallback).", "write", svg_export,
         {"source": "str", "output": "str", "width": "optional int", "height": "optional int"}, ["art", "vector", "svg"])
    # Sound
    spec("audio.midi_render", "audio", "Render a MIDI file to WAV with a .sf2 soundfont (FluidSynth).", "write", audio_midi_render,
         {"midi": "str", "soundfont": "str", "output": "str", "sample_rate": "optional int", "gain": "optional float"}, ["audio", "midi", "music"])
    spec("audio.puredata_run", "audio", "Run a Pure Data patch headless; reports audio files it wrote.", "exec", audio_puredata_run,
         {"patch": "str", "timeout_sec": "optional int"}, ["audio", "synthesis", "puredata"])
    spec("audio.supercollider_run", "audio", "Run a SuperCollider .scd script (e.g. NRT render); reports audio it wrote.", "exec", audio_supercollider_run,
         {"script": "str", "timeout_sec": "optional int"}, ["audio", "synthesis", "supercollider"])