"""Blender integration (Phase 4.2). Optional: everything else in CIE works without Blender.

CIE drives Blender in the background only (owner decision): a hidden `blender -b` process with
factory settings and auto-run scripts disabled runs CIE's own job script (`blender_job.py`).
Nothing is sent to a Blender window you have open, and Python embedded in .blend files never runs.

  blender.status     find Blender, report version, engines, glTF/FBX support
  blender.process    booleans, real bevels, remesh, Catmull-Clark subdivision, smoothing,
                     decimate, solidify, mirror, array, noise displacement, procedural
                     realistic materials; export GLB / FBX (Roblox) / OBJ / .blend
  blender.render     Eevee (default), Cycles or Workbench renders with studio lighting
  blender.bake       bake a detailed version (or a separate high-poly) onto the game mesh:
                     normal, AO, colour and roughness maps, packed into the GLB
  blender.materials  the procedural material presets

Results land in CIE's studio folder and go through CIE's own mesh checks and preview sheet,
with Blender's renders added to the sheet.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .config import get_cie_home, load_config

JOB_SCRIPT = Path(__file__).with_name("blender_job.py")
ENGINES = ("eevee", "cycles", "workbench")
VIEW_PRESETS = {
    "hero": [{"name": "hero", "yaw": 35, "pitch": 18}],
    "turntable": [{"name": f"turn{a:03d}", "yaw": a, "pitch": 12} for a in (0, 90, 180, 270)],
    "sheet": [{"name": "three_quarter", "yaw": 35, "pitch": 18}, {"name": "front", "yaw": 0, "pitch": 5},
              {"name": "side", "yaw": 90, "pitch": 5}, {"name": "back", "yaw": 200, "pitch": 15}],
    "portrait": [{"name": "portrait", "yaw": 20, "pitch": 5, "lens": 85}, {"name": "profile", "yaw": 90, "pitch": 0, "lens": 85}],
}
OPS = ("bevel", "boolean", "subdivide", "remesh", "decimate", "weld", "smooth", "solidify", "mirror", "array", "displace",
       "triangulate", "shade_smooth", "shade_flat", "material")
MATERIAL_PRESETS = ("skin", "wood", "metal", "gold", "stone", "plastic", "fabric", "rubber", "glass", "eye")


class BlenderMissing(RuntimeError):
    pass


def find_blender() -> str:
    from .diagnostics import detect_blender
    exe = detect_blender()
    if not exe:
        raise BlenderMissing(
            "Blender was not found. Install Blender 4.2 or newer from blender.org (the normal installer puts it where CIE looks), "
            "or set blender_path in CIE's config. Everything else in CIE works without it.")
    return exe


def _jobs_dir() -> Path:
    d = get_cie_home() / "studio" / "blender_jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _argv(exe: str, job_dir: Path) -> list[str]:
    # -b: no window; --factory-startup: ignore the user's prefs/add-ons; --disable-autoexec: never run
    # Python embedded in .blend files. The only script that runs is CIE's own job script.
    return [exe, "-b", "--factory-startup", "--disable-autoexec", "--python", str(JOB_SCRIPT), "--", str(job_dir)]


def run_job(op: str, params: dict[str, Any], timeout: int = 600, exe: str | None = None) -> dict[str, Any]:
    """Run one job in a hidden Blender. Returns the job's result dict (raises on failure)."""
    exe = exe or find_blender()
    job_dir = _jobs_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}_{op}_{uuid.uuid4().hex[:6]}"
    job_dir.mkdir(parents=True)
    params = dict(params)
    params.setdefault("out_dir", str(job_dir))
    (job_dir / "job.json").write_text(json.dumps({"op": op, "params": params}), encoding="utf-8")
    argv = _argv(exe, job_dir)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}  # don't leak CIE's Python into Blender's
    t0 = time.time()
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace",  # noqa: S603
                              timeout=max(10, min(int(timeout), 3600)), cwd=str(job_dir), env=env, check=False)
        out_tail = (proc.stdout + proc.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Blender took longer than {timeout}s and was stopped. Try a smaller size, fewer samples, or Eevee.") from None
    res_path = job_dir / "result.json"
    if not res_path.exists():
        raise RuntimeError(f"Blender exited (code {proc.returncode}) without a result. Last output:\n{out_tail[-1500:]}")
    result = json.loads(res_path.read_text(encoding="utf-8"))
    result["job_dir"] = str(job_dir)
    result["wall_seconds"] = round(time.time() - t0, 2)
    if not result.get("ok"):
        raise RuntimeError(f"Blender job failed: {result.get('error')}\n{(result.get('trace') or '')[-1200:]}")
    return result


def status(refresh: bool = False) -> dict[str, Any]:
    from .diagnostics import blender_candidates
    cfg = load_config()
    try:
        exe = find_blender()
    except BlenderMissing as exc:
        return {"available": False, "message": str(exc), "searched": [str(p) for p in blender_candidates()]}
    cache = get_cie_home() / "blender_info.json"
    key = f"{exe}|{Path(exe).stat().st_mtime if Path(exe).exists() else 0}"
    if cache.exists() and not refresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("key") == key:
                return {"available": True, "path": exe, **data["info"], "cached": True}
        except (ValueError, OSError):
            pass
    info = run_job("info", {}, timeout=120, exe=exe)["data"]
    cache.write_text(json.dumps({"key": key, "info": info}), encoding="utf-8")
    other = [str(p) for p in blender_candidates() if str(p).lower() != exe.lower()]
    out = {"available": True, "path": exe, **info, "configured_path": cfg.get("blender_path") or None}
    if other:
        out["other_installs"] = other
    if not info.get("gltf"):
        out["warning"] = "This Blender has no glTF add-on, so GLB import/export won't work."
    return out


# ---------------------------------------------------------------------------
# Studio integration
# ---------------------------------------------------------------------------


def _views(spec: Any) -> list[dict[str, Any]]:
    if spec in (None, "", True):
        return VIEW_PRESETS["sheet"]
    if isinstance(spec, str):
        if spec not in VIEW_PRESETS:
            raise ValueError(f"Unknown view preset '{spec}'. Presets: {', '.join(VIEW_PRESETS)}.")
        return VIEW_PRESETS[spec]
    return [dict(v) for v in spec]


def _render_params(args: dict[str, Any], default_size: int = 512) -> dict[str, Any] | None:
    if args.get("render") is False:
        return None
    engine = args.get("engine", "eevee")
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {', '.join(ENGINES)}.")
    return {"engine": engine, "size": max(64, min(int(args.get("size", default_size)), 2048)),
            "samples": max(1, min(int(args.get("samples", 64)), 1024)), "views": _views(args.get("views"))}


def _check_ops(ops: list[dict[str, Any]]) -> None:
    for op in ops or []:
        if op.get("op") not in OPS:
            raise ValueError(f"Unknown Blender op '{op.get('op')}'. Ops: {', '.join(OPS)}.")
        if op.get("op") == "material" and op.get("preset", "plastic") not in MATERIAL_PRESETS:
            raise ValueError(f"Unknown material preset. Presets: {', '.join(MATERIAL_PRESETS)}.")
        if op.get("op") == "boolean" and (op.get("with") or {}).get("file"):
            from .studio import resolve_input
            op["with"]["file"] = str(resolve_input(op["with"]["file"]))


def _finish(name: str, result: dict[str, Any], title: str, extra: dict[str, Any]) -> dict[str, Any]:
    """Copy Blender's outputs into the studio, run CIE's mesh checks, build the preview sheet."""
    import shutil

    from PIL import Image

    from . import mesh3d as M
    from .studio import make_sheet, out_path, save_sheet, verdict, write_manifest
    data = result["data"]
    files: list[str] = []
    for f in data.get("files", []):
        src = Path(f)
        dst = out_path("mesh", name, src.suffix)
        shutil.copy2(src, dst)
        files.append(str(dst))
    for m, f in (data.get("maps") or {}).items():
        dst = out_path("texture", f"{name}_{m}", ".png")
        shutil.copy2(f, dst)
        files.append(str(dst))
    issues: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    panels: list[tuple[str, Any]] = []
    glb = next((f for f in files if f.endswith((".glb", ".obj"))), None)
    if glb:
        m = M.load(glb)
        rep = M.validate(m)
        rep.pop("boundary")
        rep.pop("_split")
        stats = rep["stats"]
        # Smooth, subdivided or remeshed models legitimately have many thin/sliver triangles; keep those as info.
        issues = [i for i in rep["issues"] if "sliver" not in i["message"]]
        panels.append(("CIE check view", M.render(m, 256, *M.VIEWS["three_quarter"], wire=True)))
    for r in data.get("renders", []):
        view = Path(r["path"]).stem[len(name) + 1:] or "view"
        with Image.open(r["path"]) as im:
            panels.append((f"Blender {r['engine']}: {view}", im.convert("RGBA").copy()))
        dst = out_path("mesh", f"{name}_{view}", ".png")
        shutil.copy2(r["path"], dst)
        files.append(str(dst))
    for m_name, f in (data.get("maps") or {}).items():
        with Image.open(f) as im:
            panels.append((f"baked {m_name}", im.convert("RGBA").copy()))
    lines = []
    if data.get("before") and data.get("after"):
        lines.append(f"Blender: {data['before']['triangles']} -> {data['after']['triangles']} triangles; ops {', '.join(data.get('applied', [])) or 'none'}")
    if stats:
        lines.append(f"CIE check: {stats['triangles']} triangles, watertight {stats['watertight']}, holes {stats['holes']}, "
                     f"non-manifold {stats['non_manifold_edges']}, size {stats['size_m']} m")
    lines.append(f"Blender time {result.get('seconds')} s; " + "; ".join(result.get("log", [])[-3:]))
    sheet = save_sheet(make_sheet(title, panels, lines, issues, panel_size=320, cols=3), "mesh", name)
    info = {"files": files, "preview": str(sheet), "stats": stats, "issues": issues, "verdict": verdict(issues),
            "blender_seconds": result.get("seconds"), "log": result.get("log", []), **extra}
    if files:
        write_manifest(Path(files[0]), info)
    return info


def process(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(args["source"])
    ops = [dict(o) for o in args.get("ops") or []]
    _check_ops(ops)
    formats = [f.lower().lstrip(".") for f in (args.get("formats") or ["glb"])]
    for f in formats:
        if f not in ("glb", "gltf", "fbx", "obj", "blend"):
            raise ValueError(f"Unknown export format '{f}'.")
    if "glb" not in formats and "obj" not in formats:
        formats.insert(0, "glb")  # CIE checks the GLB
    name = safe_name(args.get("name") or src.stem + "_blender")
    params = {"input": str(src), "ops": ops, "formats": formats, "stem": name, "render": _render_params(args)}
    res = run_job("process", params, timeout=int(args.get("timeout", 900)))
    return _finish(name, res, f"Blender: {name}", {"ops": [o["op"] for o in ops]})


def render(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(args["source"])
    rp = _render_params({**args, "render": True}, 768)
    name = safe_name(args.get("name") or src.stem + "_render")
    params = {"input": str(src), "stem": name, **rp}
    res = run_job("render", params, timeout=int(args.get("timeout", 1200)))
    res["data"]["files"] = []
    return _finish(name, res, f"Blender render: {name} ({rp['engine']})", {"engine": rp["engine"]})


def bake(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    low = resolve_input(args["source"])
    params: dict[str, Any] = {"low": str(low), "size": max(128, min(int(args.get("size", 1024)), 4096)),
                              "maps": args.get("maps") or ["normal", "ao", "color"], "extrusion": float(args.get("extrusion", 0.02)),
                              "formats": [f.lower() for f in (args.get("formats") or ["glb"])]}
    for m in params["maps"]:
        if m not in ("normal", "ao", "color", "roughness"):
            raise ValueError("maps can be normal, ao, color, roughness.")
    if args.get("high"):
        params["high"] = str(resolve_input(args["high"]))
    for key in ("high_ops", "material_ops"):
        if args.get(key):
            ops = [dict(o) for o in args[key]]
            _check_ops(ops)
            params[key] = ops
    name = safe_name(args.get("name") or low.stem + "_baked")
    params["stem"] = name
    params["render"] = _render_params({**args, "size": args.get("render_size", 512)})
    res = run_job("bake", params, timeout=int(args.get("timeout", 1800)))
    return _finish(name, res, f"Blender bake: {name}", {"maps": list(res["data"].get("maps", {}))})


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                tail = f" Verdict: {data['verdict']}." if "verdict" in data else ""
                return {"status": "ok", "summary": summary + tail, "data": data}
            except BlenderMissing as exc:
                return {"status": "tool_missing", "summary": str(exc)}
            except (ValueError, FileNotFoundError, PermissionError, RuntimeError, KeyError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    view_doc = f"optional str|list ({', '.join(VIEW_PRESETS)} or [{{name, yaw, pitch, lens}}])"
    # These run CIE's own fixed Blender script in a hidden, factory-settings Blender and write only to
    # CIE's studio folder, so - like the other studio tools - they don't change projects.
    reg.register(CapabilitySpec(
        id="blender.status", area="3d", summary="Find Blender and report its version, render engines and glTF/FBX support (optional integration).",
        input_schema={"refresh": "optional bool"}, permission="read",
        handler=wrap(lambda a: status(bool(a.get("refresh", False))), "Blender checked."), tags=["blender", "3d", "integration"]))
    reg.register(CapabilitySpec(
        id="blender.process", area="3d",
        summary="Run a model through Blender: boolean (union/difference/intersect with a shape or file), real bevels, remesh, Catmull-Clark "
                "subdivision, smooth, decimate, solidify, mirror, array, noise displacement, procedural materials. Exports GLB/FBX/OBJ/.blend, "
                "renders with Eevee, and checks the result.",
        input_schema={"source": "str", "ops": f"list[dict]: ops {', '.join(OPS)}", "formats": "optional list[str]", "name": "optional str",
                      "render": "optional bool", "engine": "optional str (eevee|cycles|workbench)", "views": view_doc,
                      "size": "optional int", "samples": "optional int", "timeout": "optional int"},
        permission="read", handler=wrap(process, "Blender processed the model."), tags=["blender", "3d", "boolean", "bevel", "realistic"]))
    reg.register(CapabilitySpec(
        id="blender.render", area="3d",
        summary="Render a model in Blender with studio lighting: Eevee (default), Cycles or Workbench; hero, turntable, sheet or portrait views.",
        input_schema={"source": "str", "engine": "optional str", "views": view_doc, "size": "optional int", "samples": "optional int",
                      "name": "optional str", "timeout": "optional int"},
        permission="read", handler=wrap(render, "Rendered."), tags=["blender", "render", "preview"]))
    reg.register(CapabilitySpec(
        id="blender.bake", area="3d",
        summary="Bake detail into textures: normal, ambient occlusion, colour and roughness maps from a high-poly model (or from the same "
                "model made richer with high_ops / procedural material_ops) onto the game mesh; packs the maps into the GLB.",
        input_schema={"source": "str", "high": "optional str", "high_ops": "optional list[dict]", "material_ops": "optional list[dict]",
                      "maps": "optional list[str]", "size": "optional int", "extrusion": "optional float", "formats": "optional list[str]",
                      "engine": "optional str", "views": view_doc, "render": "optional bool", "render_size": "optional int",
                      "name": "optional str", "timeout": "optional int"},
        permission="read", handler=wrap(bake, "Baked."), tags=["blender", "bake", "normal map", "textures"]))
    reg.register(CapabilitySpec(
        id="blender.materials", area="3d", summary="List Blender procedural material presets, ops and view presets.",
        input_schema={}, permission="read",
        handler=lambda a: {"status": "ok", "summary": "Blender presets listed.",
                           "data": {"materials": list(MATERIAL_PRESETS), "ops": list(OPS), "views": VIEW_PRESETS, "engines": list(ENGINES)}},
        tags=["blender", "materials"]))
