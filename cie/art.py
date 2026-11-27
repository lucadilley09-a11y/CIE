"""Art and 3D intelligence subsystem: image inspector, compare, palette, sprite/tile checks, 3D formats, Blender adapter."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageChops

from .config import iter_project_files, project_roots, resolve_in_projects
from .diagnostics import detect_blender

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".gif"}
MODEL_3D_EXTS = {".obj", ".stl", ".gltf", ".glb", ".fbx", ".blend"}


def image_inspector(path: str) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    with Image.open(resolved) as im:
        has_alpha = im.mode in {"RGBA", "LA", "PA"} or ("transparency" in im.info)
        return {
            "path": str(resolved),
            "format": im.format,
            "mode": im.mode,
            "width": im.width,
            "height": im.height,
            "has_alpha": has_alpha,
            "file_size_bytes": resolved.stat().st_size,
            "info_keys": sorted(str(k) for k in im.info.keys()),
        }


def image_compare(path_a: str, path_b: str) -> dict[str, Any]:
    res_a, _ = resolve_in_projects(path_a)
    res_b, _ = resolve_in_projects(path_b)
    with Image.open(res_a) as im_a, Image.open(res_b) as im_b:
        same_size = (im_a.size == im_b.size)
        a_rgba = im_a.convert("RGBA")
        b_rgba = im_b.convert("RGBA")
        if not same_size:
            b_rgba = b_rgba.resize(a_rgba.size)
        diff = ImageChops.difference(a_rgba, b_rgba)
        arr = np.asarray(diff, dtype=np.float32)
        mae = float(np.mean(arr))
        rmse = float(math.sqrt(np.mean(arr**2)))
        bbox = diff.getbbox()
        return {
            "path_a": str(res_a),
            "path_b": str(res_b),
            "same_dimensions": same_size,
            "size_a": list(im_a.size),
            "size_b": list(im_b.size),
            "mean_abs_diff": round(mae, 4),
            "rmse": round(rmse, 4),
            "identical": rmse == 0.0 and same_size,
            "changed_bbox": list(bbox) if bbox else None,
        }


def palette_analyzer(path: str, max_colors: int = 8) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    with Image.open(resolved) as im:
        rgb = im.convert("RGB")
        quant = rgb.quantize(colors=max(2, min(32, int(max_colors))))
        pal = quant.getpalette() or []
        counts = sorted(quant.getcolors() or [], reverse=True)
        total_px = rgb.width * rgb.height or 1
        colors = []
        for count, idx in counts[:max_colors]:
            base = idx * 3
            if base + 2 < len(pal):
                r, g, b = pal[base], pal[base + 1], pal[base + 2]
                colors.append({
                    "hex": f"#{r:02X}{g:02X}{b:02X}",
                    "rgb": [r, g, b],
                    "ratio": round(count / total_px, 4),
                })
        return {"path": str(resolved), "dominant_colors": colors}


def sprite_sheet_validator(path: str, cell_width: int, cell_height: int) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    cw, ch = max(1, int(cell_width)), max(1, int(cell_height))
    with Image.open(resolved) as im:
        rgba = im.convert("RGBA")
        div_x = rgba.width % cw == 0
        div_y = rgba.height % ch == 0
        cols = rgba.width // cw
        rows = rgba.height // ch
        empty_frames = 0
        arr = np.asarray(rgba)
        for ry in range(rows):
            for cx in range(cols):
                cell = arr[ry * ch:(ry + 1) * ch, cx * cw:(cx + 1) * cw, 3]
                if np.max(cell) == 0:
                    empty_frames += 1
        return {
            "path": str(resolved),
            "sheet_size": [rgba.width, rgba.height],
            "cell_size": [cw, ch],
            "grid_aligned": bool(div_x and div_y),
            "cols": cols,
            "rows": rows,
            "total_frames": cols * rows,
            "empty_frames": empty_frames,
        }


def tile_texture_checks(path: str) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    with Image.open(resolved) as im:
        rgb = im.convert("RGB")
        arr = np.asarray(rgb, dtype=np.float32)
        h, w, _ = arr.shape
        is_pot_w = (w & (w - 1) == 0) and w > 0
        is_pot_h = (h & (h - 1) == 0) and h > 0
        horiz_diff = float(np.mean(np.abs(arr[:, 0, :] - arr[:, -1, :]))) if w > 1 else 0.0
        vert_diff = float(np.mean(np.abs(arr[0, :, :] - arr[-1, :, :]))) if h > 1 else 0.0
        return {
            "path": str(resolved),
            "width": w,
            "height": h,
            "power_of_two": bool(is_pot_w and is_pot_h),
            "horizontal_seam_diff": round(horiz_diff, 3),
            "vertical_seam_diff": round(vert_diff, 3),
            "likely_seamless": horiz_diff < 12.0 and vert_diff < 12.0,
        }


def batch_resize_convert(paths: list[str], output_dir: str, width: int | None = None, height: int | None = None, target_format: str = "PNG") -> dict[str, Any]:
    out_dir_res, _ = resolve_in_projects(output_dir)
    out_dir_res.mkdir(parents=True, exist_ok=True)
    outputs = []
    for p in paths:
        res_p, _ = resolve_in_projects(p)
        with Image.open(res_p) as im:
            work = im.copy()
            if width and height:
                work = work.resize((int(width), int(height)))
            ext = target_format.lower().replace("jpeg", "jpg")
            out_path = out_dir_res / f"{res_p.stem}.{ext}"
            work.save(out_path, format=target_format.upper())
            outputs.append(str(out_path))
    return {"converted": outputs, "count": len(outputs)}


def asset_catalog(project_id: str = "") -> dict[str, Any]:
    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    images: list[dict[str, Any]] = []
    models_3d: list[dict[str, Any]] = []
    for r in roots:
        for f in iter_project_files(r, extensions=IMAGE_EXTS | MODEL_3D_EXTS, include_binary=True):
            ext = f.suffix.lower()
            if ext in IMAGE_EXTS:
                images.append({"project_id": r["id"], "path": str(f), "ext": ext, "size_bytes": f.stat().st_size})
            else:
                models_3d.append({"project_id": r["id"], "path": str(f), "ext": ext, "size_bytes": f.stat().st_size})
    return {"images": images[:200], "models_3d": models_3d[:200]}


def inspect_3d_format(path: str) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    ext = resolved.suffix.lower()
    size = resolved.stat().st_size
    info: dict[str, Any] = {"path": str(resolved), "format": ext.lstrip("."), "size_bytes": size}
    if ext == ".obj":
        txt = resolved.read_text(encoding="utf-8", errors="replace")
        info["vertices"] = sum(1 for ln in txt.splitlines() if ln.startswith("v "))
        info["faces"] = sum(1 for ln in txt.splitlines() if ln.startswith("f "))
    elif ext == ".gltf":
        data = json.loads(resolved.read_text(encoding="utf-8", errors="replace"))
        info["meshes"] = len(data.get("meshes", []))
        info["materials"] = len(data.get("materials", []))
        info["nodes"] = len(data.get("nodes", []))
    return info


def blender_scene_report(blend_path: str) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(blend_path)
    blender = detect_blender()
    if not blender:
        return {
            "status": "unavailable",
            "path": str(resolved),
            "message": "Blender executable not detected. Install Blender or set blender_path in config.",
        }
    py_snippet = (
        "import bpy, json; "
        "d={'objects':len(bpy.data.objects),'meshes':len(bpy.data.meshes),'materials':len(bpy.data.materials),'images':len(bpy.data.images)}; "
        "print('CIE_BLENDER_JSON:' + json.dumps(d))"
    )
    proc = subprocess.run([blender, "--background", str(resolved), "--python-expr", py_snippet], capture_output=True, text=True, timeout=45)
    for ln in proc.stdout.splitlines():
        if ln.startswith("CIE_BLENDER_JSON:"):
            return {"status": "ok", "path": str(resolved), "scene": json.loads(ln.split(":", 1)[1])}
    return {"status": "error", "path": str(resolved), "stderr": proc.stderr[-400:]}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="art.image_inspector",
        area="art",
        summary="Inspect image dimensions, format, color mode, alpha, and metadata.",
        input_schema={"path": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Image inspected.", "data": image_inspector(args["path"])},
        tags=["art", "image"],
    ))
    reg.register(CapabilitySpec(
        id="art.image_compare",
        area="art",
        summary="Compare two images for dimensions, MAE/RMSE pixel difference, and changed region.",
        input_schema={"path_a": "str", "path_b": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Images compared.", "data": image_compare(args["path_a"], args["path_b"])},
        tags=["art", "compare"],
    ))
    reg.register(CapabilitySpec(
        id="art.palette_analyzer",
        area="art",
        summary="Extract dominant hex/RGB colors and ratios from an image.",
        input_schema={"path": "str", "max_colors": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Palette analyzed.", "data": palette_analyzer(args["path"], int(args.get("max_colors", 8)))},
        tags=["art", "palette"],
    ))
    reg.register(CapabilitySpec(
        id="art.sprite_validate",
        area="art",
        summary="Validate sprite-sheet grid alignment and count empty frames.",
        input_schema={"path": "str", "cell_width": "int", "cell_height": "int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Sprite sheet validated.", "data": sprite_sheet_validator(args["path"], int(args["cell_width"]), int(args["cell_height"]))},
        tags=["art", "sprite"],
    ))
    reg.register(CapabilitySpec(
        id="art.tile_check",
        area="art",
        summary="Check power-of-two dimensions and seamless edge differences for textures.",
        input_schema={"path": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Texture tile checked.", "data": tile_texture_checks(args["path"])},
        tags=["art", "texture"],
    ))
    reg.register(CapabilitySpec(
        id="art.batch_convert",
        area="art",
        summary="Resize or convert approved image assets into an output directory.",
        input_schema={"paths": "list[str]", "output_dir": "str", "width": "optional int", "height": "optional int", "target_format": "optional str"},
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Batch image conversion finished.", "data": batch_resize_convert(args.get("paths") or [], args["output_dir"], args.get("width"), args.get("height"), args.get("target_format", "PNG"))},
        tags=["art", "batch"],
    ))
    reg.register(CapabilitySpec(
        id="art.asset_catalog",
        area="art",
        summary="Catalog image and 3D assets across approved project folders.",
        input_schema={"project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Asset catalog built.", "data": asset_catalog(args.get("project_id", ""))},
        tags=["art", "3d", "catalog"],
    ))
    reg.register(CapabilitySpec(
        id="art.inspect_3d",
        area="art",
        summary="Inspect 3D model metadata (.obj, .gltf, .glb, .stl, .fbx, .blend).",
        input_schema={"path": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "3D format inspected.", "data": inspect_3d_format(args["path"])},
        tags=["art", "3d"],
    ))
    reg.register(CapabilitySpec(
        id="art.blender_report",
        area="art",
        summary="Inspect a .blend scene using installed Blender in background mode.",
        input_schema={"path": "str"},
        permission="read",
        handler=lambda args: {"status": blender_scene_report(args["path"]).get("status", "ok"), "summary": "Blender inspection completed.", "data": blender_scene_report(args["path"])},
        tags=["art", "blender", "3d"],
    ))