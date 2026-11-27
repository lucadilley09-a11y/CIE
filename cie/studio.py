"""Creative studio shared services (Phase 3).

Everything CIE's creative tools make goes into CIE's own studio folder
(CIE_HOME/studio/<kind>/). Nothing in a project changes until the owner
approves `studio.place`, which snapshots first like every other write.

This module also builds the preview sheets Claude inspects before calling an
asset done, and serves them to Claude through the `cie_view` MCP tool.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import get_cie_home, resolve_in_projects

KINDS = ("mesh", "rig", "pixel", "texture", "audio", "sheets")
PREVIEW_MAX = 1024


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def studio_root() -> Path:
    root = get_cie_home() / "studio"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_name(name: str, default: str = "asset") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "").strip()).strip("._")
    return (cleaned or default)[:64]


def out_path(kind: str, name: str, ext: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"Unknown studio kind '{kind}'.")
    folder = studio_root() / kind
    folder.mkdir(parents=True, exist_ok=True)
    ext = ext if ext.startswith(".") else "." + ext
    return folder / f"{safe_name(name)}{ext}"


def is_studio_path(path: Path) -> bool:
    try:
        path.resolve().relative_to(studio_root().resolve())
        return True
    except ValueError:
        return False


def resolve_input(raw: str) -> Path:
    """Inputs may come from the studio folder or from a registered project."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("A file path is required.")
    if ".." in raw.replace("\\", "/").split("/"):
        raise PermissionError("Path traversal ('..') is not permitted.")
    cand = Path(raw).expanduser()
    if not cand.is_absolute():
        studio_cand = studio_root() / cand
        if studio_cand.exists():
            return studio_cand.resolve()
    if cand.is_absolute() and is_studio_path(cand):
        if not cand.exists():
            raise FileNotFoundError(f"'{raw}' does not exist.")
        return cand.resolve()
    resolved, _ = resolve_in_projects(raw)
    if not resolved.exists():
        raise FileNotFoundError(f"'{raw}' does not exist.")
    return resolved


def write_manifest(path: Path, info: dict[str, Any]) -> Path:
    """Sidecar JSON next to every studio output: how it was made, its checks, its files."""
    side = path.with_suffix(path.suffix + ".cie.json")
    payload = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), **info}
    side.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return side


# ---------------------------------------------------------------------------
# Preview sheets
# ---------------------------------------------------------------------------

_BG = (24, 26, 31, 255)
_PANEL = (38, 41, 48, 255)
_TEXT = (226, 229, 235, 255)
_DIM = (150, 156, 168, 255)
_RED = (255, 107, 107, 255)
_YEL = (255, 206, 86, 255)
_GRN = (110, 220, 140, 255)


def _font(size: int = 12) -> ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def checker(w: int, h: int, cell: int = 8) -> Image.Image:
    yy, xx = np.mgrid[0:h, 0:w]
    on = ((xx // cell + yy // cell) % 2).astype(bool)
    arr = np.where(on[..., None], np.array([62, 66, 75, 255], np.uint8), np.array([48, 51, 59, 255], np.uint8))
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def fit(img: Image.Image, box: int, pixel_art: bool = False) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    scale = min(box / max(w, 1), box / max(h, 1))
    if pixel_art and scale >= 1:
        scale = max(1, int(scale))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    return img.resize((nw, nh), Image.NEAREST if pixel_art else Image.LANCZOS)


def make_sheet(
    title: str,
    panels: Iterable[tuple[str, Image.Image]],
    lines: Iterable[str] = (),
    issues: Iterable[dict[str, Any]] = (),
    panel_size: int = 256,
    cols: int = 4,
    pixel_art: bool = False,
) -> Image.Image:
    """A labelled grid of views plus the asset's metrics and issues."""
    panels = list(panels)
    lines = list(lines)
    issues = list(issues)
    cols = max(1, min(cols, len(panels) or 1))
    rows = (len(panels) + cols - 1) // cols
    pad, label_h = 10, 18
    text_lines = len(lines) + len(issues) + 1
    text_h = 16 * text_lines + 12
    width = pad + cols * (panel_size + pad)
    height = 34 + rows * (panel_size + label_h + pad) + text_h
    sheet = Image.new("RGBA", (width, height), _BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 9), title, fill=_TEXT, font=_font(15))
    f12 = _font(12)
    for i, (label, img) in enumerate(panels):
        r, c = divmod(i, cols)
        x0 = pad + c * (panel_size + pad)
        y0 = 34 + r * (panel_size + label_h + pad)
        bg = checker(panel_size, panel_size)
        sheet.alpha_composite(bg, (x0, y0))
        shown = fit(img, panel_size, pixel_art=pixel_art)
        ox = x0 + (panel_size - shown.width) // 2
        oy = y0 + (panel_size - shown.height) // 2
        sheet.alpha_composite(shown, (ox, oy))
        draw.text((x0 + 2, y0 + panel_size + 2), label, fill=_DIM, font=f12)
    y = 34 + rows * (panel_size + label_h + pad)
    for ln in lines:
        draw.text((pad, y), ln, fill=_TEXT, font=f12)
        y += 16
    if issues:
        for iss in issues:
            sev = iss.get("severity", "warn")
            col = _RED if sev == "error" else _YEL
            draw.text((pad, y), f"[{sev}] {iss.get('message', '')}", fill=col, font=f12)
            y += 16
    else:
        draw.text((pad, y), "No issues found by the automatic checks.", fill=_GRN, font=f12)
    return sheet


def save_sheet(sheet: Image.Image, kind: str, name: str) -> Path:
    path = out_path("sheets", f"{kind}_{name}", ".png")
    sheet.convert("RGB").save(path, optimize=True)
    return path


def verdict(issues: list[dict[str, Any]]) -> str:
    if any(i.get("severity") == "error" for i in issues):
        return "fail"
    if issues:
        return "warn"
    return "pass"


# ---------------------------------------------------------------------------
# cie_view: hand an image to Claude
# ---------------------------------------------------------------------------

def load_view(path: str, max_size: int = PREVIEW_MAX) -> tuple[bytes, dict[str, Any]]:
    """PNG bytes of a studio or project image, downscaled for Claude to look at."""
    resolved = resolve_input(path)
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        raise ValueError("cie_view shows images only. Run studio.review on other assets to get a preview sheet.")
    with Image.open(resolved) as im:
        im.seek(0)
        img = im.convert("RGBA")
    w, h = img.size
    max_size = max(64, min(int(max_size), 2048))
    pixel_art = max(w, h) <= 128
    if max(w, h) > max_size:
        img = fit(img, max_size)
    elif pixel_art:
        scale = max(1, max_size // max(w, h, 1) // 2)
        img = img.resize((w * scale, h * scale), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), {"path": str(resolved), "source_size": [w, h], "shown_size": list(img.size)}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def list_outputs(kind: str = "", limit: int = 50) -> dict[str, Any]:
    root = studio_root()
    kinds = [kind] if kind else list(KINDS)
    items: list[dict[str, Any]] = []
    for k in kinds:
        folder = root / k
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if p.is_file() and not p.name.endswith(".cie.json"):
                items.append({"kind": k, "path": str(p), "bytes": p.stat().st_size, "modified": p.stat().st_mtime})
    items.sort(key=lambda x: x["modified"], reverse=True)
    return {"studio": str(root), "count": len(items), "items": items[: max(1, int(limit))]}


def place(source: str, dest: str, overwrite: bool = False, include_sidecars: bool = False) -> dict[str, Any]:
    """Copy a studio output into a registered project (owner-approved write)."""
    src = resolve_input(source)
    if not is_studio_path(src):
        raise PermissionError("studio.place copies files from CIE's studio folder only.")
    target, root = resolve_in_projects(dest)
    if (target.exists() and target.is_dir()) or (not target.exists() and not target.suffix) or dest.endswith(("/", "\\")):
        target = target / src.name  # a folder (existing, or new when it has no file extension)
    if target.exists() and not overwrite:
        raise ValueError(f"'{target}' already exists. Pass overwrite=true to replace it (a snapshot is taken first).")
    pairs = [(src, target)] + [(c, target.parent / c.name) for c in _companions(src)]
    if include_sidecars:
        side = src.with_suffix(src.suffix + ".cie.json")
        if side.exists():
            pairs.append((side, target.with_suffix(target.suffix + ".cie.json")))
    pairs = [(a, b) for a, b in pairs if a == src or overwrite or not b.exists()]
    # Snapshot the exact files this copy creates or replaces, so an undo (or a workflow rollback)
    # removes new files and restores replaced ones.
    from .snapstore import create_snapshot
    snap = create_snapshot(paths=[str(b) for _, b in pairs], kind="auto", description="Before studio.place")
    target.parent.mkdir(parents=True, exist_ok=True)
    copied = []
    for a, b in pairs:
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(a, b)
        copied.append(str(b))
    return {"project": root.get("id"), "copied": copied, "snapshot": snap["snapshot_id"]}


def _companions(src: Path) -> list[Path]:
    out: list[Path] = []
    stem = src.with_suffix("")
    if src.suffix.lower() == ".obj":
        for ext in (".mtl",):
            p = stem.with_suffix(ext)
            if p.exists():
                out.append(p)
        out.extend(sorted(src.parent.glob(f"{src.stem}_tex*.png")))
    if src.suffix.lower() == ".png":
        p = stem.with_suffix(".json")
        if p.exists():
            out.append(p)
    return out


def review(path: str) -> dict[str, Any]:
    """Run the right quality checks for any asset and build its preview sheet."""
    resolved = resolve_input(path)
    ext = resolved.suffix.lower()
    if ext in {".glb", ".gltf", ".obj", ".stl"}:
        from . import mesh3d
        return mesh3d.review_file(str(resolved))
    if ext in {".png", ".gif", ".webp", ".bmp", ".jpg", ".jpeg"}:
        from . import pixel
        return pixel.review_file(str(resolved))
    if ext == ".wav":
        from . import synth
        return synth.review_file(str(resolved))
    raise ValueError(f"No reviewer for '{ext}' files.")


def _place_handler(a: dict[str, Any]) -> dict[str, Any]:
    try:
        data = place(a["source"], a["dest"], bool(a.get("overwrite", False)), bool(a.get("include_sidecars", False)))
    except (ValueError, FileNotFoundError, PermissionError, KeyError) as exc:
        return {"status": "error", "summary": str(exc)}
    return {"status": "ok", "summary": f"Placed {len(data['copied'])} file(s) in the project.", "data": data,
            "evidence": [{"auto_snapshot": data["snapshot"], "files": len(data["copied"])}]}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return {"status": "ok", "summary": summary, "data": fn(args)}
            except (ValueError, FileNotFoundError, PermissionError, RuntimeError, KeyError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    reg.register(CapabilitySpec(
        id="studio.list", area="studio",
        summary="List assets the creative studio has made (3D, rigs, pixel art, textures, audio, preview sheets).",
        input_schema={"kind": "optional str", "limit": "optional int"}, permission="read",
        handler=wrap(lambda a: list_outputs(a.get("kind", ""), int(a.get("limit", 50))), "Studio outputs listed."),
        tags=["studio", "assets"],
    ))
    reg.register(CapabilitySpec(
        id="studio.review", area="studio",
        summary="Quality-check any asset (glb/gltf/obj/stl, png/gif, wav) and build a preview sheet. Look at the sheet with cie_view before calling an asset done.",
        input_schema={"path": "str"}, permission="read",
        handler=wrap(lambda a: review(a["path"]), "Asset reviewed."),
        tags=["studio", "qa", "preview"],
    ))
    reg.register(CapabilitySpec(
        id="studio.place", area="studio",
        summary="Copy a finished studio asset into a project folder (with its MTL/textures/atlas JSON). Snapshots first.",
        input_schema={"source": "str", "dest": "str", "overwrite": "optional bool", "include_sidecars": "optional bool"},
        permission="write",
        handler=_place_handler,
        tags=["studio", "export"],
    ))
