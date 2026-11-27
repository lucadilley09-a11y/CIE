"""2D art for CIE (Phase 3A): pixel art first, plus general raster and vector work.

  palettes    OKLCH ramps with hue shifting, colour harmonies, Lospec .hex / GIMP .gpl export
  canvas      layered RGBA canvas driven by JSON ops: pixel, line, rect, ellipse, polygon,
              flood fill, dithered gradient, outline, auto-shade, symmetry, stamp, replace
  sprites     8-direction animated sprite sheets rendered from CIE's 3D rigs and models, so a
              character looks the same in every frame and direction
  tilesets    seamless terrain tiles and 16-tile dual-grid transition sets
  sheets      pack frames into a sheet + Aseprite-style JSON; GIF export; onion-skin preview
  textures    tileable material textures (pixel or smooth)
  UI kit      9-slice panels, buttons (3 states), slots and bars with slice metadata
  editing     adjust (OKLCH), filters, resize, crop/trim, background removal, quantise, pixelate
  vector      shapes -> SVG file + anti-aliased PNG
  quality     palette discipline, stray pixels, soft alpha, grid alignment, JPEG artefacts, seams
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .mesh3d import parse_color, value_noise

# ---------------------------------------------------------------------------
# OKLab / OKLCH
# ---------------------------------------------------------------------------


def _srgb_lin(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """rgb: (...,3) sRGB 0-1."""
    c = _srgb_lin(np.asarray(rgb, dtype=np.float64))
    l = 0.4122214708 * c[..., 0] + 0.5363325363 * c[..., 1] + 0.0514459929 * c[..., 2]
    m = 0.2119034982 * c[..., 0] + 0.6806995451 * c[..., 1] + 0.1073969566 * c[..., 2]
    s = 0.0883024619 * c[..., 0] + 0.2817188376 * c[..., 1] + 0.6299787005 * c[..., 2]
    l, m, s = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    return np.stack([0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
                     1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
                     0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s], axis=-1)


def oklab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """OKLab -> sRGB 0-1 (clipped)."""
    return _lin_srgb(_oklab_to_linear(lab))


def _oklab_to_linear(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return np.stack([r, g, bb], axis=-1)


def oklch_to_rgb(L: float, C: float, H: float) -> np.ndarray:
    """sRGB for an OKLCH colour; out-of-gamut colours lose chroma (lightness and hue are kept)."""
    h = math.radians(H)

    def inside(c: float) -> bool:
        lin = _oklab_to_linear(np.array([L, c * math.cos(h), c * math.sin(h)]))
        return bool(np.all(lin >= -1e-5) and np.all(lin <= 1 + 1e-5))

    if inside(C):
        c = C
    else:
        lo, hi = 0.0, C
        for _ in range(28):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if inside(mid) else (lo, mid)
        c = lo
    return oklab_to_rgb(np.array([L, c * math.cos(h), c * math.sin(h)]))


def rgb_to_oklch(rgb: Iterable[float]) -> tuple[float, float, float]:
    L, a, b = rgb_to_oklab(np.array(list(rgb)[:3]))
    return float(L), float(math.hypot(a, b)), float(math.degrees(math.atan2(b, a)) % 360)


def hexc(rgb: Iterable[float]) -> str:
    return "#" + "".join(f"{int(round(float(v) * 255)):02x}" for v in list(rgb)[:3])


def to_rgba8(color: Any) -> tuple[int, int, int, int]:
    c = parse_color(color)
    return tuple(int(round(v * 255)) for v in c)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------


def _hue_toward(h: float, target: float, max_deg: float) -> float:
    d = ((target - h + 540) % 360) - 180
    return (h + max(-max_deg, min(max_deg, d))) % 360


def ramp(base: Any, steps: int = 5, hue_shift: float = 0.35, contrast: float = 1.0) -> list[str]:
    """Pixel-art colour ramp around a base colour, dark to light. Shadows lean cool (towards blue),
    highlights lean warm (towards yellow), chroma peaks in the mid-tones, as hand-made palettes do.
    hue_shift 0.35 turns the ends about 20 degrees."""
    L0, C0, H0 = rgb_to_oklch(parse_color(base))
    steps = max(2, min(int(steps), 16))
    out = []
    k = float(contrast)
    max_deg = float(hue_shift) * 60.0
    for i in range(steps):
        t = i / (steps - 1) * 2 - 1  # -1 dark .. +1 light
        if t >= 0:
            L = L0 + (0.96 - L0) * t * 0.8 * k
        else:
            L = L0 - (L0 - 0.14) * (-t) * 0.78 * k
        C = C0 * (1 - 0.45 * t * t) + 0.008
        H = _hue_toward(H0, 85.0, max_deg * t) if t > 0 else _hue_toward(H0, 265.0, max_deg * (-t))
        out.append(hexc(oklch_to_rgb(min(0.98, max(0.08, L)), C, H)))
    return out


HARMONIES = {"mono": [0], "complementary": [0, 180], "analogous": [-30, 0, 30], "triadic": [0, 120, 240],
             "split": [0, 150, 210], "tetradic": [0, 90, 180, 270]}


def harmony(base: Any, scheme: str = "complementary", steps: int = 5, hue_shift: float = 0.35,
            neutrals: bool = True) -> dict[str, list[str]]:
    if scheme not in HARMONIES:
        raise ValueError(f"Unknown scheme '{scheme}'. Schemes: {', '.join(HARMONIES)}.")
    L, C, H = rgb_to_oklch(parse_color(base))
    out: dict[str, list[str]] = {}
    for i, off in enumerate(HARMONIES[scheme]):
        hue = (H + off) % 360
        c = C if i == 0 else C * 0.9
        out[f"ramp{i}"] = ramp(hexc(oklch_to_rgb(L, c, hue)), steps, hue_shift)
    if neutrals:
        out["neutral"] = ramp(hexc(oklch_to_rgb(0.55, 0.02, H)), steps, 0.15)
    return out


def flatten_palette(p: Any) -> list[str]:
    if isinstance(p, dict):
        cols = [c for v in p.values() for c in v]
    else:
        cols = list(p)
    seen: list[str] = []
    for c in cols:
        h = hexc(parse_color(c))
        if h not in seen:
            seen.append(h)
    return seen


def palette_image(p: list[str], cell: int = 16) -> Image.Image:
    img = Image.new("RGBA", (cell * max(1, len(p)), cell), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, c in enumerate(p):
        d.rectangle([i * cell, 0, (i + 1) * cell - 1, cell - 1], fill=to_rgba8(c))
    return img


def write_palette(p: list[str], name: str) -> list[str]:
    from .studio import out_path
    png = out_path("pixel", f"{name}_palette", ".png")
    palette_image(p, 1).save(png)
    hx = out_path("pixel", f"{name}_palette", ".hex")
    hx.write_text("\n".join(c.lstrip("#") for c in p) + "\n", encoding="utf-8")
    gpl = out_path("pixel", f"{name}_palette", ".gpl")
    lines = ["GIMP Palette", f"Name: {name}", "Columns: 8", "#"]
    for c in p:
        r, g, b, _ = to_rgba8(c)
        lines.append(f"{r:3d} {g:3d} {b:3d}\t{c}")
    gpl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(png), str(hx), str(gpl)]


def quantize(img: np.ndarray, palette: list[str], dither: str = "none") -> np.ndarray:
    """Map every opaque pixel to its nearest palette colour (OKLab distance). dither: none | ordered."""
    pal = np.array([parse_color(c)[:3] for c in palette])
    pal_lab = rgb_to_oklab(pal)
    rgba = img.astype(np.float64) / 255.0
    lab = rgb_to_oklab(rgba[..., :3])
    if dither == "ordered":
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0 - 0.5
        h, w = img.shape[:2]
        tile = np.tile(bayer, (h // 4 + 1, w // 4 + 1))[:h, :w]
        lab = lab.copy()
        lab[..., 0] += tile * 0.06
    d = ((lab[..., None, :] - pal_lab[None, None, :, :]) ** 2).sum(axis=-1)
    idx = np.argmin(d, axis=-1)
    out = img.copy()
    out[..., :3] = (pal[idx] * 255 + 0.5).astype(np.uint8)
    out[..., 3] = np.where(img[..., 3] >= 128, 255, 0)
    return out


# ---------------------------------------------------------------------------
# Canvas + drawing ops
# ---------------------------------------------------------------------------


class Canvas:
    def __init__(self, w: int, h: int, palette: list[str] | None = None) -> None:
        if not (1 <= w <= 4096 and 1 <= h <= 4096):
            raise ValueError("Canvas must be 1-4096 px each way.")
        self.w, self.h = int(w), int(h)
        self.layers: list[dict[str, Any]] = [{"name": "base", "px": np.zeros((self.h, self.w, 4), np.uint8), "opacity": 1.0, "visible": True}]
        self.cur = 0
        self.palette = palette or []
        self.symmetry: str = ""

    @property
    def px(self) -> np.ndarray:
        return self.layers[self.cur]["px"]

    def color(self, c: Any) -> tuple[int, int, int, int]:
        if isinstance(c, int) and self.palette:
            return to_rgba8(self.palette[c % len(self.palette)])
        if isinstance(c, str) and c.startswith("p") and c[1:].isdigit() and self.palette:
            return to_rgba8(self.palette[int(c[1:]) % len(self.palette)])
        return to_rgba8(c)

    def put(self, mask: np.ndarray, col: tuple[int, int, int, int]) -> None:
        if self.symmetry in ("x", "xy"):
            mask = mask | mask[:, ::-1]
        if self.symmetry in ("y", "xy"):
            mask = mask | mask[::-1, :]
        if col[3] == 0:
            self.px[mask] = 0
        elif col[3] == 255:
            self.px[mask] = col
        else:
            a = col[3] / 255.0
            base = self.px[mask].astype(np.float64)
            base[:, :3] = base[:, :3] * (1 - a) + np.array(col[:3]) * a
            base[:, 3] = np.maximum(base[:, 3], col[3])
            self.px[mask] = base.astype(np.uint8)

    def mask_from_draw(self, fn: Any) -> np.ndarray:
        im = Image.new("L", (self.w, self.h), 0)
        fn(ImageDraw.Draw(im))
        return np.array(im) > 0

    def composite(self) -> np.ndarray:
        out = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        for layer in self.layers:
            if not layer["visible"]:
                continue
            im = Image.fromarray(layer["px"], "RGBA")
            if layer["opacity"] < 1:
                a = np.array(im)
                a[..., 3] = (a[..., 3] * layer["opacity"]).astype(np.uint8)
                im = Image.fromarray(a, "RGBA")
            out.alpha_composite(im)
        return np.array(out)


def _xy(v: Any) -> tuple[int, int]:
    return int(round(float(v[0]))), int(round(float(v[1])))


def _bresenham(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        pts.append((x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return pts


def _flood(px: np.ndarray, x: int, y: int, tol: int = 0) -> np.ndarray:
    h, w = px.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return np.zeros((h, w), bool)
    target = px[y, x].astype(np.int32)
    diff = np.abs(px.astype(np.int32) - target).max(axis=2) <= tol
    mask = np.zeros((h, w), bool)
    stack = [(x, y)]
    while stack:
        cx, cy = stack.pop()
        if mask[cy, cx] or not diff[cy, cx]:
            continue
        # scanline
        lx = cx
        while lx > 0 and diff[cy, lx - 1] and not mask[cy, lx - 1]:
            lx -= 1
        rx = cx
        while rx < w - 1 and diff[cy, rx + 1] and not mask[cy, rx + 1]:
            rx += 1
        mask[cy, lx:rx + 1] = True
        for ny in (cy - 1, cy + 1):
            if 0 <= ny < h:
                row = diff[ny, lx:rx + 1] & ~mask[ny, lx:rx + 1]
                for ox in np.nonzero(row)[0]:
                    stack.append((lx + int(ox), ny))
    return mask


BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0


def outline_mask(alpha: np.ndarray, diagonal: bool = False) -> np.ndarray:
    a = alpha > 0
    grow = np.zeros_like(a)
    shifts = [(0, 1), (0, -1), (1, 0), (-1, 0)] + ([(1, 1), (1, -1), (-1, 1), (-1, -1)] if diagonal else [])
    for dy, dx in shifts:
        grow |= np.roll(np.roll(a, dy, axis=0), dx, axis=1) & _no_wrap(a.shape, dy, dx)
    return grow & ~a


def _no_wrap(shape: tuple[int, int], dy: int, dx: int) -> np.ndarray:
    m = np.ones(shape, bool)
    if dy > 0:
        m[:dy, :] = False
    elif dy < 0:
        m[dy:, :] = False
    if dx > 0:
        m[:, :dx] = False
    elif dx < 0:
        m[:, dx:] = False
    return m


def auto_shade(px: np.ndarray, ramp_cols: list[str], light: tuple[float, float] = (-1.0, -1.0), region: np.ndarray | None = None) -> None:
    """Shade a flat shape with a ramp: pixels facing the light get lighter colours, the far side darker,
    the rim darkest. Works on the shape's own silhouette, like hand shading."""
    from scipy import ndimage
    shape = px[..., 3] > 0 if region is None else region
    if not shape.any():
        return
    dist = ndimage.distance_transform_edt(shape)
    gy, gx = np.gradient(ndimage.gaussian_filter(dist, 1.0))
    lx, ly = light
    ln = math.hypot(lx, ly) or 1
    facing = -(gx * lx + gy * ly) / ln  # inward gradient points away from the edge
    facing = facing / (np.abs(facing[shape]).max() or 1)
    depth = np.clip(dist / (dist.max() or 1), 0, 1)
    tone = 0.5 + 0.45 * facing + 0.25 * (depth - 0.5)
    tone[(dist <= 1.0) & shape] -= 0.25  # rim
    idx = np.clip((tone * len(ramp_cols)).astype(int), 0, len(ramp_cols) - 1)
    cols = np.array([to_rgba8(c) for c in ramp_cols], np.uint8)
    px[shape] = cols[idx[shape]]


def run_ops(cv: Canvas, ops: list[dict[str, Any]]) -> Canvas:
    for op in ops or []:
        kind = op.get("op")
        col = cv.color(op["color"]) if "color" in op else (0, 0, 0, 255)
        if kind == "layer":
            name = op.get("name", f"layer{len(cv.layers)}")
            existing = [i for i, l in enumerate(cv.layers) if l["name"] == name]
            if existing:
                cv.cur = existing[0]
            else:
                cv.layers.append({"name": name, "px": np.zeros((cv.h, cv.w, 4), np.uint8), "opacity": float(op.get("opacity", 1.0)), "visible": True})
                cv.cur = len(cv.layers) - 1
            if "opacity" in op:
                cv.layers[cv.cur]["opacity"] = float(op["opacity"])
            if "visible" in op:
                cv.layers[cv.cur]["visible"] = bool(op["visible"])
        elif kind == "symmetry":
            cv.symmetry = op.get("axis", "")
        elif kind == "pixel":
            m = np.zeros((cv.h, cv.w), bool)
            for p in op.get("points") or [op.get("at")]:
                x, y = _xy(p)
                if 0 <= x < cv.w and 0 <= y < cv.h:
                    m[y, x] = True
            cv.put(m, col)
        elif kind == "line":
            m = np.zeros((cv.h, cv.w), bool)
            pts = op.get("points") or [op["from"], op["to"]]
            for a, b in zip(pts[:-1], pts[1:]):
                for x, y in _bresenham(*_xy(a), *_xy(b)):
                    if 0 <= x < cv.w and 0 <= y < cv.h:
                        m[y, x] = True
            cv.put(m, col)
        elif kind in ("rect", "ellipse"):
            x0, y0 = _xy(op["from"])
            x1, y1 = _xy(op["to"])
            box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
            fill = op.get("fill", True)
            if kind == "rect":
                m = cv.mask_from_draw(lambda d: d.rectangle(box, fill=255 if fill else None, outline=255))
            else:
                m = cv.mask_from_draw(lambda d: d.ellipse(box, fill=255 if fill else None, outline=255))
            cv.put(m, col)
        elif kind == "polygon":
            pts = [tuple(_xy(p)) for p in op["points"]]
            m = cv.mask_from_draw(lambda d: d.polygon(pts, fill=255 if op.get("fill", True) else None, outline=255))
            cv.put(m, col)
        elif kind == "fill":
            x, y = _xy(op["at"])
            cv.put(_flood(cv.px, x, y, int(op.get("tolerance", 0))), col)
        elif kind == "replace":
            src = np.array(cv.color(op["from_color"]), np.uint8)
            m = np.all(cv.px == src, axis=2)
            cv.put(m, cv.color(op["to_color"]))
        elif kind == "gradient":
            # dithered gradient between palette entries over a rectangle (ordered Bayer)
            x0, y0 = _xy(op["from"])
            x1, y1 = _xy(op["to"])
            cols = [cv.color(c) for c in op["colors"]]
            vertical = op.get("direction", "vertical") == "vertical"
            for y in range(max(0, min(y0, y1)), min(cv.h, max(y0, y1) + 1)):
                for x in range(max(0, min(x0, x1)), min(cv.w, max(x0, x1) + 1)):
                    t = (y - min(y0, y1)) / max(1, abs(y1 - y0)) if vertical else (x - min(x0, x1)) / max(1, abs(x1 - x0))
                    pos = t * (len(cols) - 1)
                    i = int(pos)
                    frac = pos - i
                    j = min(i + 1, len(cols) - 1)
                    pick = cols[j] if frac > BAYER4[y % 4, x % 4] else cols[i]
                    cv.px[y, x] = pick
        elif kind == "outline":
            m = outline_mask(cv.px[..., 3], bool(op.get("diagonal", False)))
            cv.put(m, col)
        elif kind == "selout":
            # selective outline: darken the shape's own edge pixels instead of adding a hard black line
            a = cv.px[..., 3] > 0
            edge = a & outline_mask(~a * 255, False)
            factor = float(op.get("factor", 0.55))
            cv.px[edge, :3] = (cv.px[edge, :3] * factor).astype(np.uint8)
        elif kind == "shade":
            region = None
            if "region_color" in op:
                region = np.all(cv.px == np.array(cv.color(op["region_color"]), np.uint8), axis=2)
            auto_shade(cv.px, [hexc(np.array(cv.color(c)[:3]) / 255) for c in op["ramp"]], tuple(op.get("light", (-1, -1))), region)
        elif kind == "noise":
            rng = np.random.default_rng(int(op.get("seed", 0)))
            a = cv.px[..., 3] > 0
            hit = a & (rng.random((cv.h, cv.w)) < float(op.get("density", 0.1)))
            cv.put(hit, col)
        elif kind == "shift":
            dx, dy = _xy(op.get("by", (0, 0)))
            cv.layers[cv.cur]["px"] = np.roll(np.roll(cv.px, dy, axis=0), dx, axis=1) if op.get("wrap") else _shift(cv.px, dx, dy)
        elif kind == "flip":
            ax = 1 if op.get("axis", "x") == "x" else 0
            cv.layers[cv.cur]["px"] = np.flip(cv.px, axis=ax).copy()
        elif kind == "rotate90":
            k = int(op.get("times", 1)) % 4
            if cv.w != cv.h and k % 2:
                raise ValueError("rotate90 by an odd count needs a square canvas.")
            cv.layers[cv.cur]["px"] = np.rot90(cv.px, -k).copy()
        elif kind == "copy":
            x0, y0 = _xy(op["from"])
            x1, y1 = _xy(op["to"])
            dx, dy = _xy(op["dest"])
            block = cv.px[y0:y1 + 1, x0:x1 + 1].copy()
            if op.get("flip") == "x":
                block = block[:, ::-1]
            _paste(cv.px, block, dx, dy)
        elif kind == "stamp":
            from .studio import resolve_input
            with Image.open(resolve_input(op["source"])) as im:
                block = np.array(im.convert("RGBA"))
            x, y = _xy(op.get("at", (0, 0)))
            _paste(cv.px, block, x, y)
        elif kind == "clear":
            cv.layers[cv.cur]["px"][:] = 0
        else:
            raise ValueError(f"Unknown pixel op '{kind}'. Ops: layer, symmetry, pixel, line, rect, ellipse, polygon, fill, replace, "
                             "gradient, outline, selout, shade, noise, shift, flip, rotate90, copy, stamp, clear.")
    return cv


def _shift(px: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(px)
    h, w = px.shape[:2]
    ys, yd = (slice(0, h - dy), slice(dy, h)) if dy >= 0 else (slice(-dy, h), slice(0, h + dy))
    xs, xd = (slice(0, w - dx), slice(dx, w)) if dx >= 0 else (slice(-dx, w), slice(0, w + dx))
    out[yd, xd] = px[ys, xs]
    return out


def _paste(dst: np.ndarray, block: np.ndarray, x: int, y: int) -> None:
    h, w = dst.shape[:2]
    bh, bw = block.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + bw), min(h, y + bh)
    if x0 >= x1 or y0 >= y1:
        return
    src = block[y0 - y:y1 - y, x0 - x:x1 - x]
    a = src[..., 3:4].astype(np.float64) / 255
    region = dst[y0:y1, x0:x1].astype(np.float64)
    region[..., :3] = region[..., :3] * (1 - a) + src[..., :3] * a
    region[..., 3] = np.maximum(region[..., 3], src[..., 3])
    dst[y0:y1, x0:x1] = region.astype(np.uint8)


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def analyse_image(px: np.ndarray, pixel_art: bool | None = None, palette: list[str] | None = None,
                  cell: tuple[int, int] | None = None, tileable: bool = False, tiles: bool = False) -> dict[str, Any]:
    """tiles=True: cells are terrain tiles (they are meant to fill their cell edge to edge)."""
    h, w = px.shape[:2]
    alpha = px[..., 3]
    opaque = alpha > 0
    if pixel_art is None:
        pixel_art = max(w, h) <= 256 and len(np.unique(px[opaque][:, :3].reshape(-1, 3), axis=0)) <= 64 if opaque.any() else True
    issues: list[dict[str, Any]] = []
    cols = np.unique(px[opaque][:, :3], axis=0) if opaque.any() else np.zeros((0, 3))
    m: dict[str, Any] = {"size": [w, h], "colors": int(len(cols)), "opaque_fraction": round(float(opaque.mean()), 4), "pixel_art": bool(pixel_art)}
    soft = (alpha > 0) & (alpha < 255)
    m["semi_transparent_pixels"] = int(soft.sum())
    if pixel_art:
        if soft.sum():
            issues.append({"severity": "warn", "message": f"{int(soft.sum())} semi-transparent pixels; pixel art should use fully opaque or fully clear pixels (engines filter soft edges into halos)."})
        if len(cols) > 32:
            issues.append({"severity": "warn", "message": f"{len(cols)} colours is a lot for a sprite; limit to a palette (quantize)."})
        # orphan pixels: opaque pixel whose colour differs from all 8 neighbours and that touches transparency or noise
        if opaque.any() and min(w, h) >= 3:
            rgb = px[..., :3].astype(np.int32)
            same = np.zeros((h, w), np.int32)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    sh = np.roll(np.roll(rgb, dy, 0), dx, 1)
                    sa = np.roll(np.roll(opaque, dy, 0), dx, 1)
                    same += (np.all(sh == rgb, axis=2) & sa & _no_wrap((h, w), dy, dx)).astype(np.int32)
            neigh = np.zeros((h, w), np.int32)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy or dx:
                        neigh += (np.roll(np.roll(opaque, dy, 0), dx, 1) & _no_wrap((h, w), dy, dx)).astype(np.int32)
            lonely = opaque & (neigh <= 1)
            orphans = opaque & (same == 0) & (neigh <= 3)
            m["stray_pixels"] = int(lonely.sum())
            m["orphan_pixels"] = int(orphans.sum())
            if lonely.sum():
                issues.append({"severity": "warn", "message": f"{int(lonely.sum())} stray pixels floating on their own."})
            if orphans.sum() > max(3, opaque.sum() * 0.01):
                issues.append({"severity": "warn", "message": f"{int(orphans.sum())} orphan pixels on edges (noisy outline); clean them up."})
    else:
        edge = outline_mask(alpha, True) | (opaque & outline_mask((~opaque) * 255, True))
        if opaque.any() and edge.any() and alpha.min() == 0:
            aa = float(((alpha > 0) & (alpha < 255) & edge).sum()) / max(1, int((edge & opaque).sum()))
            m["edge_antialiasing"] = round(aa, 3)
            if aa < 0.05 and max(w, h) > 128:
                issues.append({"severity": "warn", "message": "Edges are hard (no anti-aliasing); they will look jagged at this size."})
        if w >= 16 and h >= 16:
            g = px[..., :3].astype(np.float64).mean(axis=2)
            dx = np.abs(np.diff(g, axis=1))
            at8 = dx[:, 7::8].mean() if dx[:, 7::8].size else 0
            other = dx.mean() or 1e-9
            m["jpeg_blockiness"] = round(float(at8 / other), 3)
            if at8 / other > 1.6 and other > 0.5:
                issues.append({"severity": "warn", "message": "8x8 block edges detected: JPEG compression artefacts. Use the PNG source."})
    if palette:
        pal = {tuple(to_rgba8(c)[:3]) for c in palette}
        off = [c for c in map(tuple, cols.tolist()) if c not in pal]
        m["off_palette_colors"] = len(off)
        if off:
            issues.append({"severity": "warn", "message": f"{len(off)} colours are not in the palette (e.g. {hexc(np.array(off[0]) / 255)})."})
    if cell:
        cw, ch = cell
        if w % cw or h % ch:
            issues.append({"severity": "error", "message": f"Sheet {w}x{h} is not a whole number of {cw}x{ch} cells."})
        else:
            bases, lefts = [], []
            for cy in range(h // ch):
                for cx in range(w // cw):
                    a = alpha[cy * ch:(cy + 1) * ch, cx * cw:(cx + 1) * cw] > 0
                    if a.any():
                        ys, xs = np.nonzero(a)
                        bases.append(ys.max())
                        lefts.append((xs.min() + xs.max()) / 2)
                        if ys.min() == 0 or xs.min() == 0 or ys.max() == ch - 1 or xs.max() == cw - 1:
                            m["frames_touching_cell_edge"] = m.get("frames_touching_cell_edge", 0) + 1
            if bases:
                m["baseline_spread_px"] = int(max(bases) - min(bases))
                m["centre_spread_px"] = round(float(max(lefts) - min(lefts)), 1)
                if m["baseline_spread_px"] > max(2, ch // 8) and not tiles:
                    issues.append({"severity": "warn", "message": f"Frames' feet/baselines jump by {m['baseline_spread_px']} px between cells; align them so the sprite doesn't jitter."})
            if m.get("frames_touching_cell_edge") and not tiles:
                issues.append({"severity": "warn", "message": f"{m['frames_touching_cell_edge']} frames touch their cell edge (may bleed into neighbours when filtered)."})
    if tileable:
        # The wrap-around pair (last column -> first) should look like any other neighbouring pair.
        f = px[..., :3].astype(np.int32)
        cols = np.abs(np.diff(f, axis=1)).mean(axis=(0, 2))
        rows = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
        wrap_x = float(np.abs(f[:, 0] - f[:, -1]).mean())
        wrap_y = float(np.abs(f[0] - f[-1]).mean())
        ratio = max(wrap_x / (float(cols.max()) + 1e-6), wrap_y / (float(rows.max()) + 1e-6))
        m["seam_ratio"] = round(ratio, 3)
        if ratio > 1.25:
            issues.append({"severity": "warn", "message": f"Visible seam when tiled: the wrap-around edge differs {ratio:.2f}x more than any other pair of neighbouring rows/columns."})
    from .studio import verdict
    return {"metrics": m, "issues": issues, "verdict": verdict(issues)}


def _tile2(px: np.ndarray) -> np.ndarray:
    return np.tile(px, (2, 2, 1))


def preview_image(px: np.ndarray, name: str, title: str, analysis: dict[str, Any], palette: list[str] | None = None,
                  extra: list[tuple[str, Image.Image]] | None = None, tileable: bool = False) -> str:
    from .studio import make_sheet, save_sheet
    img = Image.fromarray(px, "RGBA")
    pixel_art = analysis["metrics"].get("pixel_art", True)
    panels = [("on checker", img)]
    dark = Image.new("RGBA", img.size, (20, 20, 24, 255))
    dark.alpha_composite(img)
    light = Image.new("RGBA", img.size, (235, 235, 230, 255))
    light.alpha_composite(img)
    panels += [("on dark", dark), ("on light", light)]
    if tileable:
        panels.append(("tiled 2x2", Image.fromarray(_tile2(px), "RGBA")))
    if palette:
        panels.append((f"palette ({len(palette)})", palette_image(palette, 16)))
    if pixel_art and analysis["metrics"].get("orphan_pixels"):
        hl = np.array(dark)
        rgb = px[..., :3].astype(np.int32)
        opaque = px[..., 3] > 0
        same = np.zeros(px.shape[:2], np.int32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    same += np.all(np.roll(np.roll(rgb, dy, 0), dx, 1) == rgb, axis=2)
        hl[opaque & (same == 0)] = (255, 40, 200, 255)
        panels.append(("orphans (pink)", Image.fromarray(hl, "RGBA")))
    panels += list(extra or [])
    mt = analysis["metrics"]
    lines = [f"{mt['size'][0]}x{mt['size'][1]} px, {mt['colors']} colours, {'pixel art' if pixel_art else 'smooth art'}, verdict {analysis['verdict']}"]
    detail = {k: v for k, v in mt.items() if k not in ("size", "colors", "pixel_art")}
    lines.append(", ".join(f"{k} {v}" for k, v in detail.items())[:180])
    sheet = make_sheet(title, panels, lines, analysis["issues"], panel_size=256, cols=4, pixel_art=pixel_art)
    return str(save_sheet(sheet, "pixel", name))


def review_file(path: str) -> dict[str, Any]:
    p = Path(path)
    with Image.open(p) as im:
        n = getattr(im, "n_frames", 1)
        im.seek(0)
        px = np.array(im.convert("RGBA"))
        frames = []
        if n > 1:
            for i in range(min(n, 24)):
                im.seek(i)
                frames.append(im.convert("RGBA").copy())
    meta_json = p.with_suffix(".json")
    cell = None
    if meta_json.exists():
        try:
            meta = json.loads(meta_json.read_text(encoding="utf-8"))
            cs = meta.get("meta", {}).get("cell")
            if cs:
                cell = (int(cs[0]), int(cs[1]))
        except (ValueError, OSError):
            pass
    tileable = "tile" in p.stem.lower() or "texture" in str(p.parent).lower()
    an = analyse_image(px, cell=cell, tileable=tileable)
    extra = [(f"frame {i}", f) for i, f in enumerate(frames[:6])] if frames else []
    if frames:
        an["metrics"]["frames"] = n
    return {"path": str(p), "preview": preview_image(px, "review_" + p.stem, f"Review: {p.name}", an, extra=extra, tileable=tileable), **an}


# ---------------------------------------------------------------------------
# Finishing helper
# ---------------------------------------------------------------------------


def save_px(px: np.ndarray, name: str, kind: str = "pixel") -> Path:
    from .studio import out_path
    p = out_path(kind, name, ".png")
    Image.fromarray(px, "RGBA").save(p, optimize=True)
    return p


def finish_image(px: np.ndarray, name: str, title: str, palette: list[str] | None = None, cell: tuple[int, int] | None = None,
                 tileable: bool = False, tiles: bool = False, extra_files: list[str] | None = None, extra_panels: list[tuple[str, Image.Image]] | None = None,
                 info: dict[str, Any] | None = None, kind: str = "pixel", pixel_art: bool | None = None) -> dict[str, Any]:
    from .studio import write_manifest
    p = save_px(px, name, kind)
    an = analyse_image(px, pixel_art, palette, cell, tileable, tiles)
    sheet = preview_image(px, name, title, an, palette, extra_panels, tileable)
    out = {"files": [str(p)] + list(extra_files or []), "preview": sheet, **an, **(info or {})}
    write_manifest(p, {k: v for k, v in out.items()})
    return out


# ---------------------------------------------------------------------------
# Sprites from 3D (8 directions, consistent across frames)
# ---------------------------------------------------------------------------

DIRECTIONS_8 = ["S", "SW", "W", "NW", "N", "NE", "E", "SE"]  # S faces the camera


def render_sprite(mesh: Any, size: int, yaw: float, pitch: float, fit: tuple, palette: list[str] | None,
                  outline: str | None = "#1a1420", supersample: int = 4) -> np.ndarray:
    from . import mesh3d as M
    big = M.render(mesh, size * supersample, yaw, pitch, fit_bounds=fit, show_backfaces=False)
    arr = np.array(big)
    # majority/area downsample: average colour of covered sub-pixels, coverage decides opacity
    h = w = size
    a = arr.reshape(h, supersample, w, supersample, 4).astype(np.float64)
    cov = (a[..., 3] > 0).mean(axis=(1, 3))
    rgb = (a[..., :3] * (a[..., 3:4] > 0)).sum(axis=(1, 3)) / np.maximum((a[..., 3] > 0).sum(axis=(1, 3))[..., None], 1)
    out = np.zeros((h, w, 4), np.uint8)
    on = cov >= 0.45
    out[..., :3] = rgb.astype(np.uint8)
    out[..., 3] = np.where(on, 255, 0)
    if palette:
        out = quantize(out, palette)
    if outline:
        m = outline_mask(out[..., 3], False)
        out[m] = to_rgba8(outline)
    return out


def sprite_palette_from_mesh(mesh: Any, steps: int = 4) -> list[str]:
    cols: list[str] = []
    for mat in mesh.materials:
        cols += ramp(mat["base_color"], steps, 0.3, 0.8)
    return flatten_palette(cols)


def sprite_sheet_8dir(args: dict[str, Any]) -> dict[str, Any]:
    from . import mesh3d as M
    from . import rig as R
    from .studio import out_path, resolve_input, safe_name
    size = int(args.get("size", 32))
    dirs = int(args.get("directions", 8))
    if dirs not in (1, 4, 8):
        raise ValueError("directions must be 1, 4 or 8.")
    pitch = float(args.get("pitch", 30))
    name = safe_name(args.get("name") or "sprite")
    frames_per = int(args.get("frames", 6))
    poses: list[Any] = []
    clip_name = args.get("clip", "walk")
    if args.get("rig") or args.get("character") is not None:
        if args.get("rig"):
            rig = R.load_rig(args["rig"])
        else:
            c = args.get("character") or {}
            rig = R.build_humanoid(name, float(c.get("height", 1.8)), c.get("build", "chibi"), c.get("style", "blocky"),
                                   c.get("colors"), c.get("hair", "short"), c.get("accessory", ""))
        clip = R.make_clips(rig, [clip_name])[0]
        idx = np.linspace(0, clip.frames - 1 - (1 if clip.loop else 0), frames_per).round().astype(int)
        for f in idx:
            poses.append(R.pose_mesh(rig, clip.rots[f], clip.root[f]))
        base_mesh = rig.mesh
    else:
        base_mesh = M.load(resolve_input(args["source"]))
        poses = [base_mesh]
        frames_per = 1
    los = np.min([p.bounds()[0] for p in poses], axis=0)
    his = np.max([p.bounds()[1] for p in poses], axis=0)
    span = float((his - los).max())
    c = (los + his) / 2
    fit = (c - span * 0.36, c + span * 0.36)
    palette = flatten_palette(args["palette"]) if args.get("palette") else sprite_palette_from_mesh(base_mesh)
    if args.get("outline", "#1a1420"):
        palette = flatten_palette(palette + [args.get("outline", "#1a1420")])
    yaws = {1: [0], 4: [0, 270, 180, 90], 8: [0, 315, 270, 225, 180, 135, 90, 45]}[dirs]
    labels = {1: ["S"], 4: ["S", "W", "N", "E"], 8: DIRECTIONS_8}[dirs]
    sheet = np.zeros((size * dirs, size * frames_per, 4), np.uint8)
    for r, yaw in enumerate(yaws):
        for f, pm in enumerate(poses):
            sheet[r * size:(r + 1) * size, f * size:(f + 1) * size] = render_sprite(pm, size, yaw, pitch, fit, palette, args.get("outline", "#1a1420"))
    fps = int(args.get("fps", 10))
    meta = {"frames": {}, "meta": {"image": f"{name}.png", "size": {"w": sheet.shape[1], "h": sheet.shape[0]}, "cell": [size, size],
                                   "frameTags": [], "app": "CIE studio"}}
    for r, lab in enumerate(labels):
        meta["meta"]["frameTags"].append({"name": f"{clip_name}_{lab}", "from": r * frames_per, "to": r * frames_per + frames_per - 1, "direction": "forward"})
        for f in range(frames_per):
            meta["frames"][f"{clip_name}_{lab}_{f}"] = {"frame": {"x": f * size, "y": r * size, "w": size, "h": size}, "duration": int(1000 / fps)}
    jp = out_path("pixel", name, ".json")
    jp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    gif = out_path("pixel", f"{name}_preview", ".gif")
    gif_frames = []
    for f in range(frames_per):
        row = Image.fromarray(sheet[:, f * size:(f + 1) * size], "RGBA")
        strip = Image.new("RGBA", (size * dirs, size), (40, 43, 50, 255))
        for r in range(dirs):
            strip.alpha_composite(row.crop((0, r * size, size, (r + 1) * size)), (r * size, 0))
        gif_frames.append(strip.resize((size * dirs * 3, size * 3), Image.NEAREST).convert("P", palette=Image.Palette.ADAPTIVE))
    gif_frames[0].save(gif, save_all=True, append_images=gif_frames[1:], duration=int(1000 / fps), loop=0)
    pal_files = write_palette(palette, name)
    return finish_image(sheet, name, f"Sprite sheet: {name} ({dirs} directions x {frames_per} frames, {size}px)", palette,
                        (size, size), extra_files=[str(jp), str(gif)] + pal_files,
                        info={"directions": labels, "frames_per_direction": frames_per, "cell": [size, size],
                              "note": "Rows are directions (S faces the camera), columns are frames. JSON follows Aseprite's hash format."})


# ---------------------------------------------------------------------------
# Textures and tilesets
# ---------------------------------------------------------------------------

TERRAIN = {
    "grass": ("#5f9e3a", "noise", "tufts"), "dirt": ("#7a5534", "noise", "pebbles"), "sand": ("#d9c08a", "noise", "specks"),
    "stone": ("#8a8a86", "stone", "cracks"), "water": ("#3d7fb8", "waves", ""), "snow": ("#e6edf3", "noise", "specks"),
    "lava": ("#e0561b", "waves", "glow"), "wood": ("#9a6b3f", "planks", ""), "brick": ("#9c4a36", "brick", ""),
    "cobble": ("#7d7f86", "cobble", ""), "metal": ("#9aa0a8", "plate", ""),
}


def texture(kind: str, size: int = 32, seed: int = 0, pixel: bool = True, colors: int = 5, base: Any = None) -> tuple[np.ndarray, list[str]]:
    """Tileable texture. pixel=True quantises to a hue-shifted ramp for a hand-made look."""
    from .mesh3d import pattern_texture
    if kind not in TERRAIN:
        raise ValueError(f"Unknown texture '{kind}'. Kinds: {', '.join(TERRAIN)}.")
    col, pattern, detail = TERRAIN[kind]
    col = base or col
    s = int(size)
    rng = np.random.default_rng(int(seed))
    rgb = np.array(parse_color(col)[:3])
    if pattern == "waves":
        yy, xx = np.mgrid[0:s, 0:s] / s
        n = value_noise(s, s, 3, seed, 3)
        f = 0.85 + 0.2 * np.sin((yy * 4 + n * 1.5) * 2 * np.pi)
        px = np.concatenate([np.clip(rgb * f[..., None], 0, 1), np.ones((s, s, 1))], axis=2)
        px = (px * 255).astype(np.uint8)
    elif pattern == "cobble":
        # cells from tileable noise: wrapped voronoi
        pts = rng.random((9, 2)) * s
        yy, xx = np.mgrid[0:s, 0:s]
        d1 = np.full((s, s), 1e9)
        d2 = np.full((s, s), 1e9)
        ids = np.zeros((s, s), int)
        for i, (px_, py_) in enumerate(pts):
            for ox in (-s, 0, s):
                for oy in (-s, 0, s):
                    d = np.hypot(xx - px_ - ox, yy - py_ - oy)
                    closer = d < d1
                    d2 = np.where(closer, d1, np.minimum(d2, d))
                    ids = np.where(closer, i, ids)
                    d1 = np.minimum(d1, d)
        edge = (d2 - d1) < s * 0.06
        tone = 0.85 + 0.25 * rng.random(9)[ids] + 0.1 * (value_noise(s, s, 4, seed) - 0.5)
        f = np.where(edge, 0.55, tone)
        px = (np.concatenate([np.clip(rgb * f[..., None], 0, 1), np.ones((s, s, 1))], axis=2) * 255).astype(np.uint8)
    elif pattern == "plate":
        n = value_noise(s, s, 6, seed, 2)
        f = 0.9 + 0.1 * n
        f[[0, s // 2], :] = 0.65
        f[:, [0, s // 2]] = 0.65
        f[[1, s // 2 + 1], :] *= 1.15
        for y0 in (3, s // 2 + 3):
            for x0 in (3, s // 2 + 3):
                if y0 < s and x0 < s:
                    f[y0, x0] = 1.3
        px = (np.concatenate([np.clip(rgb * f[..., None], 0, 1), np.ones((s, s, 1))], axis=2) * 255).astype(np.uint8)
    else:
        px = pattern_texture(pattern, rgb, s, seed)
    if detail and pixel:
        n_det = max(1, s * s // 90)
        for _ in range(n_det):
            x, y = int(rng.integers(0, s)), int(rng.integers(0, s))
            if detail == "tufts":
                for dy, dx in ((0, 0), (-1, 0), (-1, -1), (0, 1)):
                    px[(y + dy) % s, (x + dx) % s, :3] = np.clip(rgb * 1.25 * 255, 0, 255)
            elif detail in ("pebbles", "specks"):
                px[y, x, :3] = np.clip(rgb * (0.6 if detail == "pebbles" else 1.3) * 255, 0, 255)
            elif detail == "cracks":
                for k in range(3):
                    px[(y + k) % s, (x + (k if rng.random() > 0.5 else 0)) % s, :3] = np.clip(rgb * 0.55 * 255, 0, 255)
            elif detail == "glow":
                px[y, x, :3] = (255, 220, 120)
    palette: list[str] = []
    if pixel:
        # fine grain so the quantised tile reads as a surface rather than a flat fill
        grain = 0.82 + 0.36 * value_noise(s, s, max(4, s // 4), seed + 5, 2)
        px[..., :3] = np.clip(px[..., :3] * grain[..., None], 0, 255).astype(np.uint8)
        palette = ramp(col, max(3, int(colors)), 0.35, 0.45)
        if detail == "glow":
            palette.append("#ffdc78")
        px = quantize(px, palette)
    return px, palette


def _corner_mask(size: int, corners: tuple[int, int, int, int], seed: int, roughness: float) -> np.ndarray:
    """corners = (TL, TR, BL, BR) membership of the upper terrain. Seamless across tiles because the
    noise field is periodic with the tile and the corner blend matches on shared edges."""
    s = size
    yy, xx = (np.mgrid[0:s, 0:s] + 0.5) / s
    tl, tr, bl, br = corners
    v = tl * (1 - xx) * (1 - yy) + tr * xx * (1 - yy) + bl * (1 - xx) * yy + br * xx * yy
    n = value_noise(s, s, 2, seed, 3) - 0.5
    return (v + n * float(roughness)) > 0.5


def tileset_dual_grid(upper: str, lower: str, size: int = 16, seed: int = 0, roughness: float = 0.45) -> tuple[np.ndarray, dict[str, Any], list[str]]:
    """16-tile dual-grid transition set (4x4). Tile index = TL*8 + TR*4 + BL*2 + BR (1 = upper terrain)."""
    up, pal_u = texture(upper, size, seed)
    lo, pal_l = texture(lower, size, seed + 1)
    s = int(size)
    sheet = np.zeros((s * 4, s * 4, 4), np.uint8)
    mapping = {}
    edge_col = np.array(to_rgba8(ramp(TERRAIN[upper][0], 5)[0]), np.uint8)
    for idx in range(16):
        corners = ((idx >> 3) & 1, (idx >> 2) & 1, (idx >> 1) & 1, idx & 1)
        mask = _corner_mask(s, corners, seed + 7, roughness)
        tile = np.where(mask[..., None], up, lo)
        rim = mask & outline_mask((~mask).astype(np.uint8) * 255, False)
        tile[rim] = edge_col
        r, c = divmod(idx, 4)
        sheet[r * s:(r + 1) * s, c * s:(c + 1) * s] = tile
        mapping[str(idx)] = {"tl": corners[0], "tr": corners[1], "bl": corners[2], "br": corners[3], "x": c * s, "y": r * s}
    meta = {"layout": "dual-grid 4x4", "tile_size": s, "upper": upper, "lower": lower,
            "index_rule": "index = TL*8 + TR*4 + BL*2 + BR; 1 means the corner is the upper terrain",
            "usage": "Place display tiles on a grid offset by half a tile from the world grid; each display tile reads the 4 world cells around its corners.",
            "tiles": mapping}
    return sheet, meta, flatten_palette(pal_u + pal_l + [hexc(edge_col[:3] / 255)])


# ---------------------------------------------------------------------------
# Sheets, animation, GIF, onion skin
# ---------------------------------------------------------------------------


def pack_frames(frames: list[np.ndarray], names: list[str], columns: int = 0, padding: int = 0,
                align: str = "bottom") -> tuple[np.ndarray, dict[str, Any]]:
    """Grid-pack frames into equal cells (largest frame size); aligns each frame bottom-centre so the sprite doesn't jitter."""
    cw = max(f.shape[1] for f in frames)
    ch = max(f.shape[0] for f in frames)
    n = len(frames)
    cols = columns or int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))
    sheet = np.zeros((rows * (ch + padding), cols * (cw + padding), 4), np.uint8)
    meta: dict[str, Any] = {"frames": {}, "meta": {"size": {"w": sheet.shape[1], "h": sheet.shape[0]}, "cell": [cw + padding, ch + padding], "app": "CIE studio"}}
    for i, (f, nm) in enumerate(zip(frames, names)):
        r, c = divmod(i, cols)
        ox = c * (cw + padding) + (cw - f.shape[1]) // 2
        oy = r * (ch + padding) + ((ch - f.shape[0]) if align == "bottom" else (ch - f.shape[0]) // 2)
        sheet[oy:oy + f.shape[0], ox:ox + f.shape[1]] = f
        meta["frames"][nm] = {"frame": {"x": c * (cw + padding), "y": r * (ch + padding), "w": cw, "h": ch}, "duration": 100}
    return sheet, meta


def onion_skin(frames: list[np.ndarray], i: int) -> np.ndarray:
    base = np.zeros_like(frames[i])
    out = Image.fromarray(base, "RGBA")
    for j, tint in ((i - 1, (255, 80, 80)), (i + 1, (80, 220, 120))):
        if 0 <= j < len(frames):
            g = frames[j].copy()
            g[..., :3] = (g[..., :3] * 0.3 + np.array(tint) * 0.7).astype(np.uint8)
            g[..., 3] = (g[..., 3] * 0.35).astype(np.uint8)
            out.alpha_composite(Image.fromarray(g, "RGBA"))
    out.alpha_composite(Image.fromarray(frames[i], "RGBA"))
    return np.array(out)


def save_gif(frames: list[np.ndarray], path: Path, durations: list[int], scale: int = 1, bg: tuple = (0, 0, 0, 0)) -> None:
    imgs = []
    for f in frames:
        im = Image.fromarray(f, "RGBA")
        if scale > 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        canvas = Image.new("RGBA", im.size, bg)
        canvas.alpha_composite(im)
        p = canvas.convert("RGBA")
        alpha = np.array(p)[..., 3]
        q = p.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        if (alpha < 128).any():
            qa = np.array(q)
            qa[alpha < 128] = 255
            q = Image.fromarray(qa.astype(np.uint8), "P")
            q.putpalette(p.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).getpalette())
            q.info["transparency"] = 255
        imgs.append(q)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=durations, loop=0, disposal=2,
                 transparency=255 if "transparency" in imgs[0].info else None)


EFFECTS = ("bob", "squash", "flash", "blink_out", "sway", "shake", "spin")


def effect_frames(px: np.ndarray, effect: str, n: int = 6, amount: float = 1.0) -> list[np.ndarray]:
    """Turn a single sprite into a short looping animation (idle bob, squash & stretch, hit flash...)."""
    h, w = px.shape[:2]
    pad = max(2, int(max(h, w) * 0.15))
    frames = []
    for i in range(n):
        t = i / n
        base = np.zeros((h + 2 * pad, w + 2 * pad, 4), np.uint8)
        img = Image.fromarray(px, "RGBA")
        if effect == "bob":
            dy = int(round(-math.sin(2 * math.pi * t) * max(1, h * 0.06) * amount))
            _paste(base, px, pad, pad + dy)
        elif effect == "squash":
            k = 1 + 0.12 * amount * math.sin(2 * math.pi * t)
            nw, nh = max(1, int(round(w / k))), max(1, int(round(h * k)))
            im2 = np.array(img.resize((nw, nh), Image.NEAREST))
            _paste(base, im2, pad + (w - nw) // 2, pad + h - nh)
        elif effect == "flash":
            f2 = px.copy()
            if i % 2 == 0:
                f2[f2[..., 3] > 0, :3] = 255
            _paste(base, f2, pad, pad)
        elif effect == "blink_out":
            f2 = px.copy()
            if i % 2 == 1:
                f2[..., 3] = 0
            _paste(base, f2, pad, pad)
        elif effect == "sway":
            ang = 8 * amount * math.sin(2 * math.pi * t)
            im2 = np.array(img.rotate(ang, Image.NEAREST, expand=False, center=(w / 2, h)))
            _paste(base, im2, pad, pad)
        elif effect == "shake":
            dx = int(round(math.sin(2 * math.pi * t * 2) * 2 * amount))
            _paste(base, px, pad + dx, pad)
        elif effect == "spin":
            k = abs(math.cos(math.pi * t))
            nw = max(1, int(round(w * k)))
            im2 = np.array(img.resize((nw, h), Image.NEAREST))
            if math.cos(math.pi * t) < 0:
                im2 = im2[:, ::-1]
            _paste(base, im2, pad + (w - nw) // 2, pad)
        else:
            raise ValueError(f"Unknown effect '{effect}'. Effects: {', '.join(EFFECTS)}.")
        frames.append(base)
    return frames


# ---------------------------------------------------------------------------
# UI kit (9-slice)
# ---------------------------------------------------------------------------


def ui_kit(base: Any = "#3b4a6b", accent: Any = "#e0b44a", size: int = 24, style: str = "pixel") -> tuple[dict[str, np.ndarray], dict[str, Any], list[str]]:
    r = ramp(base, 6, 0.3)
    a = ramp(accent, 5, 0.35)
    pal = flatten_palette(r + a + ["#14101c", "#f2efe6"])
    dark, light = to_rgba8("#14101c"), to_rgba8("#f2efe6")
    s = int(size)
    border = max(3, s // 6)

    def frame(fill: str, hi: str, lo: str, inset: bool = False) -> np.ndarray:
        px = np.zeros((s, s, 4), np.uint8)
        px[:, :] = to_rgba8(fill)
        px[0, :], px[-1, :], px[:, 0], px[:, -1] = dark, dark, dark, dark
        top, bot = (lo, hi) if inset else (hi, lo)
        px[1, 1:-1] = to_rgba8(top)
        px[1:-1, 1] = to_rgba8(top)
        px[-2, 1:-1] = to_rgba8(bot)
        px[1:-1, -2] = to_rgba8(bot)
        if style == "pixel":
            for y, x in ((0, 0), (0, -1), (-1, 0), (-1, -1)):
                px[y, x] = 0
        return px

    parts = {
        "panel": frame(r[2], r[3], r[1]),
        "button_normal": frame(a[2], a[3], a[1]),
        "button_hover": frame(a[3], a[4], a[2]),
        "button_pressed": frame(a[1], a[2], a[0], inset=True),
        "slot": frame(r[1], r[0], r[2], inset=True),
    }
    bar = np.zeros((max(6, s // 3), s * 2, 4), np.uint8)
    bar[:] = to_rgba8(r[0])
    bar[0, :], bar[-1, :], bar[:, 0], bar[:, -1] = dark, dark, dark, dark
    bar_fill = bar.copy()
    red = ramp("#d64545", 3, 0.3)
    bar_fill[1:-1, 1:-1] = to_rgba8(red[1])
    bar_fill[1, 1:-1] = to_rgba8(red[2])
    pal = flatten_palette(pal + red[1:])
    parts["bar_back"], parts["bar_fill"] = bar, bar_fill
    meta = {name: {"size": [p.shape[1], p.shape[0]], "slice": {"left": border, "right": border, "top": border, "bottom": border}}
            for name, p in parts.items()}
    for k in ("bar_back", "bar_fill"):
        meta[k]["slice"] = {"left": 2, "right": 2, "top": 1, "bottom": 1}
    del light
    return parts, meta, pal


def nine_slice(px: np.ndarray, w: int, h: int, sl: dict[str, int]) -> np.ndarray:
    L, R, T, B = sl["left"], sl["right"], sl["top"], sl["bottom"]
    ph, pw = px.shape[:2]
    out = np.zeros((h, w, 4), np.uint8)
    cx = [0, L, pw - R, pw]
    cy = [0, T, ph - B, ph]
    dx = [0, L, w - R, w]
    dy = [0, T, h - B, h]
    for i in range(3):
        for j in range(3):
            src = px[cy[i]:cy[i + 1], cx[j]:cx[j + 1]]
            tw, th = dx[j + 1] - dx[j], dy[i + 1] - dy[i]
            if src.size and tw > 0 and th > 0:
                out[dy[i]:dy[i + 1], dx[j]:dx[j + 1]] = np.array(Image.fromarray(src, "RGBA").resize((tw, th), Image.NEAREST))
    return out


# ---------------------------------------------------------------------------
# Raster editing
# ---------------------------------------------------------------------------


def remove_background(px: np.ndarray, tolerance: float = 0.06, feather: int = 1) -> np.ndarray:
    """Remove a roughly uniform background connected to the image border (OKLab distance)."""
    from scipy import ndimage
    h, w = px.shape[:2]
    lab = rgb_to_oklab(px[..., :3] / 255.0)
    border = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    ref = np.median(border, axis=0)
    close = np.linalg.norm(lab - ref, axis=2) < float(tolerance)
    lbl, _ = ndimage.label(close)
    edge_labels = set(np.unique(np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]]))) - {0}
    bg = np.isin(lbl, list(edge_labels))
    out = px.copy()
    alpha = np.where(bg, 0, out[..., 3]).astype(np.float64)
    if feather > 0:
        soft = ndimage.gaussian_filter((~bg).astype(np.float64), float(feather) * 0.6)
        alpha = np.where(bg, 0, np.minimum(out[..., 3], 255 * np.clip(soft * 1.6, 0, 1)))
        # decontaminate edge colour: push away from the background colour
        bg_rgb = oklab_to_rgb(ref) * 255
        a = np.clip(alpha / 255.0, 1e-3, 1)[..., None]
        edge = (alpha > 0) & (alpha < 250)
        fixed = (out[..., :3] - bg_rgb * (1 - a)) / a
        out[edge, :3] = np.clip(fixed[edge], 0, 255).astype(np.uint8)
    out[..., 3] = alpha.astype(np.uint8)
    return out


def adjust(px: np.ndarray, lightness: float = 0.0, chroma: float = 1.0, hue: float = 0.0, contrast: float = 1.0) -> np.ndarray:
    lab = rgb_to_oklab(px[..., :3] / 255.0)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    L = (L - 0.5) * float(contrast) + 0.5 + float(lightness)
    C = np.hypot(a, b) * float(chroma)
    H = np.arctan2(b, a) + math.radians(float(hue))
    rgb = oklab_to_rgb(np.stack([L, C * np.cos(H), C * np.sin(H)], axis=-1))
    out = px.copy()
    out[..., :3] = (np.clip(rgb, 0, 1) * 255 + 0.5).astype(np.uint8)
    return out


def edit_ops(px: np.ndarray, ops: list[dict[str, Any]]) -> np.ndarray:
    for op in ops or []:
        k = op.get("op")
        img = Image.fromarray(px, "RGBA")
        if k == "resize":
            w, h = int(op.get("width", 0)), int(op.get("height", 0))
            if op.get("scale"):
                w, h = int(round(img.width * float(op["scale"]))), int(round(img.height * float(op["scale"])))
            w = w or int(round(img.width * h / img.height))
            h = h or int(round(img.height * w / img.width))
            method = {"nearest": Image.NEAREST, "smooth": Image.LANCZOS, "box": Image.BOX}.get(op.get("filter", "nearest"), Image.NEAREST)
            px = np.array(img.resize((w, h), method))
        elif k == "crop":
            x0, y0 = _xy(op["from"])
            x1, y1 = _xy(op["to"])
            px = px[y0:y1, x0:x1].copy()
        elif k == "trim":
            a = px[..., 3] > 0
            if a.any():
                ys, xs = np.nonzero(a)
                m = int(op.get("margin", 0))
                px = px[max(0, ys.min() - m):ys.max() + 1 + m, max(0, xs.min() - m):xs.max() + 1 + m].copy()
        elif k == "pad":
            n = int(op.get("pixels", 1))
            px = np.pad(px, ((n, n), (n, n), (0, 0)))
        elif k == "flip":
            px = np.flip(px, axis=1 if op.get("axis", "x") == "x" else 0).copy()
        elif k == "rotate":
            px = np.array(img.rotate(-float(op.get("degrees", 90)), Image.NEAREST if op.get("filter", "nearest") == "nearest" else Image.BICUBIC, expand=True))
        elif k == "adjust":
            px = adjust(px, float(op.get("lightness", 0)), float(op.get("chroma", 1)), float(op.get("hue", 0)), float(op.get("contrast", 1)))
        elif k == "blur":
            px = np.array(img.filter(ImageFilter.GaussianBlur(float(op.get("radius", 1)))))
        elif k == "sharpen":
            px = np.array(img.filter(ImageFilter.UnsharpMask(float(op.get("radius", 2)), int(op.get("percent", 120)), 2)))
        elif k == "remove_background":
            px = remove_background(px, float(op.get("tolerance", 0.06)), int(op.get("feather", 1)))
        elif k == "quantize":
            pal = flatten_palette(op["palette"]) if op.get("palette") else None
            if pal is None:
                q = img.convert("RGB").quantize(colors=int(op.get("colors", 16)), method=Image.Quantize.MEDIANCUT)
                pal = [hexc(np.array(q.getpalette()[i * 3:i * 3 + 3]) / 255) for i in range(int(op.get("colors", 16)))]
            px = quantize(px, pal, op.get("dither", "none"))
        elif k == "pixelate":
            # big image -> pixel art: area-average down, then limit colours
            w = int(op.get("width", 64))
            h = int(round(img.height * w / img.width))
            small = np.array(img.resize((w, h), Image.BOX))
            small[..., 3] = np.where(small[..., 3] >= 128, 255, 0)
            q = Image.fromarray(small, "RGBA").convert("RGB").quantize(colors=int(op.get("colors", 16)), method=Image.Quantize.MEDIANCUT)
            pal = [hexc(np.array(q.getpalette()[i * 3:i * 3 + 3]) / 255) for i in range(int(op.get("colors", 16)))]
            px = quantize(small, pal)
        elif k == "outline":
            m = outline_mask(px[..., 3], bool(op.get("diagonal", False)))
            if op.get("grow", True):
                px = np.pad(px, ((1, 1), (1, 1), (0, 0)))
                m = outline_mask(px[..., 3], bool(op.get("diagonal", False)))
            px[m] = to_rgba8(op.get("color", "#1a1420"))
        elif k == "drop_shadow":
            dx, dy = _xy(op.get("offset", (2, 2)))
            pad = max(abs(dx), abs(dy)) + int(op.get("blur", 0)) * 2
            big = np.pad(px, ((pad, pad), (pad, pad), (0, 0)))
            sh = np.zeros_like(big)
            sh[..., 3] = (big[..., 3] > 0) * int(255 * float(op.get("opacity", 0.5)))
            sh = _shift(sh, dx, dy)
            shim = Image.fromarray(sh, "RGBA")
            if op.get("blur"):
                shim = shim.filter(ImageFilter.GaussianBlur(float(op["blur"])))
            shim.alpha_composite(Image.fromarray(big, "RGBA"))
            px = np.array(shim)
        elif k == "glow":
            rad = float(op.get("radius", 3))
            pad = int(rad * 2)
            big = np.pad(px, ((pad, pad), (pad, pad), (0, 0)))
            g = np.zeros_like(big)
            g[..., :3] = to_rgba8(op.get("color", "#ffd36b"))[:3]
            g[..., 3] = big[..., 3]
            gi = Image.fromarray(g, "RGBA").filter(ImageFilter.GaussianBlur(rad))
            gi.alpha_composite(Image.fromarray(big, "RGBA"))
            px = np.array(gi)
        elif k == "compose":
            from .studio import resolve_input
            with Image.open(resolve_input(op["source"])) as im2:
                top = np.array(im2.convert("RGBA"))
            if op.get("opacity") is not None:
                top[..., 3] = (top[..., 3] * float(op["opacity"])).astype(np.uint8)
            x, y = _xy(op.get("at", (0, 0)))
            if op.get("below"):
                base = np.zeros_like(px)
                _paste(base, top, x, y)
                _paste(base, px, 0, 0)
                px = base
            else:
                _paste(px, top, x, y)
        elif k == "mask_circle":
            h, w = px.shape[:2]
            yy, xx = np.mgrid[0:h, 0:w]
            inside = ((xx - w / 2 + 0.5) / (w / 2)) ** 2 + ((yy - h / 2 + 0.5) / (h / 2)) ** 2 <= 1
            px[~inside, 3] = 0
        else:
            raise ValueError(f"Unknown edit op '{k}'. Ops: resize, crop, trim, pad, flip, rotate, adjust, blur, sharpen, remove_background, "
                             "quantize, pixelate, outline, drop_shadow, glow, compose, mask_circle.")
    return px


# ---------------------------------------------------------------------------
# Vector (SVG + raster)
# ---------------------------------------------------------------------------


def vector(shapes: list[dict[str, Any]], width: int, height: int, scale: int = 1) -> tuple[str, np.ndarray]:
    """shapes: rect(x,y,w,h,r), circle(cx,cy,r), ellipse, polygon(points), path(d: only M/L/Z),
    line(points), text(x,y,text,size). Fill: colour or {linear:[c1,c2], angle}. stroke, stroke_width, opacity."""
    W, H = int(width), int(height)
    ss = 4
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * scale}" height="{H * scale}" viewBox="0 0 {W} {H}">']
    defs: list[str] = []
    canvas = Image.new("RGBA", (W * scale * ss, H * scale * ss), (0, 0, 0, 0))
    k = scale * ss
    for i, sh in enumerate(shapes):
        t = sh.get("type")
        fill = sh.get("fill", "#000000")
        op = float(sh.get("opacity", 1.0))
        fill_attr, grad = "none", None
        if isinstance(fill, dict) and "linear" in fill:
            gid = f"g{i}"
            c1, c2 = fill["linear"][:2]
            ang = math.radians(float(fill.get("angle", 90)))
            x2, y2 = 0.5 + 0.5 * math.cos(ang), 0.5 + 0.5 * math.sin(ang)
            defs.append(f'<linearGradient id="{gid}" x1="{1 - x2:.3f}" y1="{1 - y2:.3f}" x2="{x2:.3f}" y2="{y2:.3f}">'
                        f'<stop offset="0" stop-color="{hexc(parse_color(c1))}"/><stop offset="1" stop-color="{hexc(parse_color(c2))}"/></linearGradient>')
            fill_attr = f"url(#{gid})"
            grad = (np.array(to_rgba8(c1), float), np.array(to_rgba8(c2), float), ang)
        elif fill not in (None, "none"):
            fill_attr = hexc(parse_color(fill))
        stroke = sh.get("stroke")
        sw = float(sh.get("stroke_width", 1))
        common = f'fill="{fill_attr}" opacity="{op}"' + (f' stroke="{hexc(parse_color(stroke))}" stroke-width="{sw}"' if stroke else "")
        mask = Image.new("L", canvas.size, 0)
        smask = Image.new("L", canvas.size, 0)
        dm, ds = ImageDraw.Draw(mask), ImageDraw.Draw(smask)
        swk = max(1, int(round(sw * k)))
        if t == "rect":
            x, y, w, h = (float(sh[c]) for c in ("x", "y", "w", "h"))
            r = float(sh.get("r", 0))
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" {common}/>')
            box = [x * k, y * k, (x + w) * k - 1, (y + h) * k - 1]
            dm.rounded_rectangle(box, radius=r * k, fill=255)
            if stroke:
                ds.rounded_rectangle(box, radius=r * k, outline=255, width=swk)
        elif t in ("circle", "ellipse"):
            cx, cy = float(sh["cx"]), float(sh["cy"])
            rx = float(sh.get("r", sh.get("rx", 1)))
            ry = float(sh.get("r", sh.get("ry", rx)))
            parts.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" {common}/>')
            box = [(cx - rx) * k, (cy - ry) * k, (cx + rx) * k, (cy + ry) * k]
            dm.ellipse(box, fill=255)
            if stroke:
                ds.ellipse(box, outline=255, width=swk)
        elif t in ("polygon", "path", "line"):
            if t == "path":
                pts, toks = [], sh["d"].replace(",", " ").split()
                i2 = 0
                while i2 < len(toks):
                    tk = toks[i2]
                    if tk.upper() in ("M", "L"):
                        pts.append((float(toks[i2 + 1]), float(toks[i2 + 2])))
                        i2 += 3
                    elif tk.upper() == "Z":
                        i2 += 1
                    else:
                        pts.append((float(toks[i2]), float(toks[i2 + 1])))
                        i2 += 2
                parts.append(f'<path d="{sh["d"]}" {common}/>')
            else:
                pts = [tuple(map(float, p)) for p in sh["points"]]
                tag = "polyline" if t == "line" else "polygon"
                parts.append(f'<{tag} points="{" ".join(f"{a},{b}" for a, b in pts)}" {common if t != "line" else common.replace(f"fill={chr(34)}{fill_attr}{chr(34)}", "fill=" + chr(34) + "none" + chr(34))}/>')
            kp = [(a * k, b * k) for a, b in pts]
            if t != "line" and len(kp) >= 3:
                dm.polygon(kp, fill=255)
            if stroke or t == "line":
                ds.line(kp + ([kp[0]] if t != "line" else []), fill=255, width=swk, joint="curve")
                if t == "line" and not stroke:
                    stroke = fill
        elif t == "text":
            from .studio import _font
            x, y = float(sh["x"]), float(sh["y"])
            size = float(sh.get("size", 12))
            parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-family="sans-serif" {common}>{_xml(sh.get("text", ""))}</text>')
            dm.text((x * k, (y - size * 0.8) * k), str(sh.get("text", "")), fill=255, font=_font(int(size * k)))
        else:
            raise ValueError(f"Unknown vector shape '{t}'.")
        if t != "line" and fill_attr != "none":
            layer = np.zeros((canvas.size[1], canvas.size[0], 4), np.float64)
            if grad is not None:
                c1, c2, ang = grad
                yy, xx = np.mgrid[0:canvas.size[1], 0:canvas.size[0]]
                proj = (xx / canvas.size[0] - 0.5) * math.cos(ang) + (yy / canvas.size[1] - 0.5) * math.sin(ang) + 0.5
                proj = np.clip(proj, 0, 1)[..., None]
                layer[...] = c1 * (1 - proj) + c2 * proj
            else:
                layer[...] = to_rgba8(fill_attr)
            layer[..., 3] = np.array(mask) / 255.0 * 255 * op
            canvas.alpha_composite(Image.fromarray(layer.astype(np.uint8), "RGBA"))
        if stroke:
            sl = np.zeros((canvas.size[1], canvas.size[0], 4), np.uint8)
            sl[...] = to_rgba8(stroke)
            sl[..., 3] = (np.array(smask) * op).astype(np.uint8)
            canvas.alpha_composite(Image.fromarray(sl, "RGBA"))
    if defs:
        parts.insert(1, "<defs>" + "".join(defs) + "</defs>")
    parts.append("</svg>")
    raster = np.array(canvas.resize((W * scale, H * scale), Image.LANCZOS))
    return "\n".join(parts), raster


def _xml(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def _palette_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import make_sheet, safe_name, save_sheet
    name = safe_name(a.get("name", "palette"))
    if a.get("scheme"):
        groups = harmony(a.get("base", "#3b6fd1"), a["scheme"], int(a.get("steps", 5)), float(a.get("hue_shift", 0.35)), bool(a.get("neutrals", True)))
    else:
        groups = {"ramp0": ramp(a.get("base", "#3b6fd1"), int(a.get("steps", 5)), float(a.get("hue_shift", 0.35)), float(a.get("contrast", 1.0)))}
    flat = flatten_palette(groups)
    files = write_palette(flat, name)
    rows = [(k, palette_image(v, 32)) for k, v in groups.items()]
    sheet = save_sheet(make_sheet(f"Palette: {name}", rows, [f"{len(flat)} colours: " + " ".join(flat[:16]) + (" ..." if len(flat) > 16 else "")], [],
                                  panel_size=256, cols=2, pixel_art=True), "pixel", name + "_palette")
    lch = [tuple(round(x, 3) for x in rgb_to_oklch(parse_color(c))) for c in flat]
    return {"files": files, "preview": str(sheet), "ramps": groups, "palette": flat, "oklch": lch, "verdict": "pass", "issues": []}


def _draw_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    pal = flatten_palette(a["palette"]) if a.get("palette") else []
    cv = Canvas(int(a.get("width", 32)), int(a.get("height", 32)), pal)
    if a.get("source"):
        from .studio import resolve_input
        with Image.open(resolve_input(a["source"])) as im:
            src = np.array(im.convert("RGBA"))
        cv = Canvas(src.shape[1], src.shape[0], pal)
        cv.layers[0]["px"] = src
    run_ops(cv, a.get("ops", []))
    px = cv.composite()
    for op in a.get("ops", []):  # colours drawn explicitly by hex are part of the intended palette
        for key in ("color", "to_color"):
            if pal and isinstance(op.get(key), str) and op[key].startswith("#") and hexc(parse_color(op[key])) not in pal:
                pal.append(hexc(parse_color(op[key])))
    name = safe_name(a.get("name", "drawing"))
    files: list[str] = []
    if a.get("save_layers"):
        for layer in cv.layers:
            p = save_px(layer["px"], f"{name}_{safe_name(layer['name'])}")
            files.append(str(p))
    return finish_image(px, name, f"Pixel art: {name}", pal or None, extra_files=files, info={"layers": [l["name"] for l in cv.layers]})


def _sprite3d_cap(a: dict[str, Any]) -> dict[str, Any]:
    return sprite_sheet_8dir(a)


def _texture_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    kind = a.get("kind", "grass")
    px, pal = texture(kind, int(a.get("size", 32)), int(a.get("seed", 0)), bool(a.get("pixel", True)), int(a.get("colors", 5)), a.get("base"))
    name = safe_name(a.get("name") or f"{kind}_tile")
    return finish_image(px, name, f"Texture: {kind}", pal or None, tileable=True, kind="texture", pixel_art=bool(a.get("pixel", True)),
                        info={"tileable": True})


def _tileset_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import out_path, safe_name
    upper, lower = a.get("upper", "grass"), a.get("lower", "dirt")
    size = int(a.get("size", 16))
    sheet, meta, pal = tileset_dual_grid(upper, lower, size, int(a.get("seed", 0)), float(a.get("roughness", 0.45)))
    name = safe_name(a.get("name") or f"{upper}_{lower}_tileset")
    meta["meta"] = {"cell": [size, size]}
    jp = out_path("pixel", name, ".json")
    jp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    # demo map: proves the tiles join without seams
    rng = np.random.default_rng(int(a.get("seed", 0)))
    world = (rng.random((9, 9)) > 0.45).astype(int)
    demo = np.zeros((8 * size, 8 * size, 4), np.uint8)
    for y in range(8):
        for x in range(8):
            idx = world[y, x] * 8 + world[y, x + 1] * 4 + world[y + 1, x] * 2 + world[y + 1, x + 1]
            r, c = divmod(idx, 4)
            demo[y * size:(y + 1) * size, x * size:(x + 1) * size] = sheet[r * size:(r + 1) * size, c * size:(c + 1) * size]
    demo_p = save_px(demo, name + "_demo")
    return finish_image(sheet, name, f"Tileset: {upper} over {lower} (dual grid)", pal, (size, size), tileable=False, tiles=True, extra_files=[str(jp), str(demo_p)],
                        extra_panels=[("demo map", Image.fromarray(demo, "RGBA"))], info={"mapping": meta["index_rule"], "usage": meta["usage"]})


def _sheet_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import out_path, resolve_input, safe_name
    frames, names = [], []
    for i, src in enumerate(a["frames"]):
        p = resolve_input(src)
        with Image.open(p) as im:
            frames.append(np.array(im.convert("RGBA")))
        names.append(f"{p.stem}_{i}")
    if a.get("trim"):
        frames = [edit_ops(f, [{"op": "trim"}]) for f in frames]
    sheet, meta = pack_frames(frames, names, int(a.get("columns", 0)), int(a.get("padding", 0)), a.get("align", "bottom"))
    dur = int(a.get("duration_ms", 100))
    for v in meta["frames"].values():
        v["duration"] = dur
    name = safe_name(a.get("name", "sheet"))
    meta["meta"]["image"] = f"{name}.png"
    jp = out_path("pixel", name, ".json")
    jp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    cw, ch = meta["meta"]["cell"]
    gif = out_path("pixel", name + "_preview", ".gif")
    cells = [sheet[v["frame"]["y"]:v["frame"]["y"] + ch, v["frame"]["x"]:v["frame"]["x"] + cw] for v in meta["frames"].values()]
    save_gif(cells, gif, [dur] * len(cells), scale=max(1, 128 // max(cw, ch)))
    return finish_image(sheet, name, f"Sprite sheet: {name}", None, (cw, ch), extra_files=[str(jp), str(gif)])


def _animate_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import out_path, resolve_input, safe_name
    name = safe_name(a.get("name", "anim"))
    pal = flatten_palette(a["palette"]) if a.get("palette") else []
    frames: list[np.ndarray] = []
    if a.get("effect"):
        with Image.open(resolve_input(a["source"])) as im:
            px = np.array(im.convert("RGBA"))
        frames = effect_frames(px, a["effect"], int(a.get("frames", 6)), float(a.get("amount", 1.0)))
    else:
        base_ops = a.get("base_ops", [])
        for i, fops in enumerate(a.get("frame_ops") or []):
            cv = Canvas(int(a.get("width", 32)), int(a.get("height", 32)), pal)
            run_ops(cv, base_ops + (fops or []))
            frames.append(cv.composite())
        if not frames:
            raise ValueError("Give an effect + source sprite, or base_ops + frame_ops (one list of ops per frame).")
    durs = a.get("durations") or [int(a.get("duration_ms", 100))] * len(frames)
    sheet, meta = pack_frames(frames, [f"{name}_{i}" for i in range(len(frames))], columns=len(frames))
    for (k, v), d in zip(meta["frames"].items(), durs):
        v["duration"] = int(d)
    meta["meta"]["frameTags"] = [{"name": name, "from": 0, "to": len(frames) - 1, "direction": "forward"}]
    jp = out_path("pixel", name, ".json")
    jp.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    gif = out_path("pixel", name + "_preview", ".gif")
    save_gif(frames, gif, [int(d) for d in durs], scale=max(1, 128 // max(frames[0].shape[:2])))
    onion = [(f"onion {i}", Image.fromarray(onion_skin(frames, i), "RGBA")) for i in range(min(len(frames), 4))]
    cw, ch = meta["meta"]["cell"]
    return finish_image(sheet, name, f"Animation: {name} ({len(frames)} frames)", pal or None, (cw, ch), extra_files=[str(jp), str(gif)],
                        extra_panels=onion, info={"frames": len(frames), "durations_ms": durs})


def _ui_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import out_path, safe_name
    parts, meta, pal = ui_kit(a.get("base", "#3b4a6b"), a.get("accent", "#e0b44a"), int(a.get("size", 24)), a.get("style", "pixel"))
    name = safe_name(a.get("name", "ui_kit"))
    files = []
    for k, px in parts.items():
        files.append(str(save_px(px, f"{name}_{k}")))
    jp = out_path("pixel", name, ".json")
    jp.write_text(json.dumps({"parts": meta, "note": "slice = 9-slice borders in pixels (Godot NinePatchRect, Roblox SliceCenter, CSS border-image)."}, indent=1), encoding="utf-8")
    demo_w, demo_h = int(a.get("size", 24)) * 6, int(a.get("size", 24)) * 4
    demo = nine_slice(parts["panel"], demo_w, demo_h, meta["panel"]["slice"])
    btn = nine_slice(parts["button_normal"], demo_w // 2, int(a.get("size", 24)), meta["button_normal"]["slice"])
    _paste(demo, btn, demo_w // 4, demo_h - int(a.get("size", 24)) - 4)
    sheet, _ = pack_frames(list(parts.values()), list(parts.keys()), columns=len(parts), padding=2)
    return finish_image(sheet, name, f"UI kit: {name}", pal, pixel_art=True, extra_files=[str(jp)] + files,
                        extra_panels=[("9-slice demo", Image.fromarray(demo, "RGBA"))], info={"parts": meta})


def _edit_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(a["source"])
    with Image.open(src) as im:
        px = np.array(im.convert("RGBA"))
    out = edit_ops(px, a.get("ops", []))
    name = safe_name(a.get("name") or src.stem + "_edit")
    return finish_image(out, name, f"Edited: {src.name}", flatten_palette(a["palette"]) if a.get("palette") else None,
                        pixel_art=a.get("pixel_art"))


def _vector_cap(a: dict[str, Any]) -> dict[str, Any]:
    from .studio import out_path, safe_name
    name = safe_name(a.get("name", "vector"))
    svg, raster = vector(a["shapes"], int(a.get("width", 64)), int(a.get("height", 64)), int(a.get("scale", 1)))
    sp = out_path("pixel", name, ".svg")
    sp.write_text(svg, encoding="utf-8")
    return finish_image(raster, name, f"Vector: {name}", extra_files=[str(sp)], pixel_art=False)


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                return {"status": "ok", "summary": f"{summary} Verdict: {data.get('verdict')}.", "data": data}
            except (ValueError, KeyError, FileNotFoundError, PermissionError, IndexError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    caps = [
        ("pixel.palette", "Generate an OKLCH pixel-art palette: hue-shifted ramps from a base colour, or a harmony (complementary, analogous, "
         "triadic, split, tetradic, mono) with neutrals. Writes PNG, Lospec .hex and GIMP/Aseprite .gpl.",
         {"base": "str", "scheme": "optional str", "steps": "optional int", "hue_shift": "optional float", "name": "optional str"}, _palette_cap,
         "Palette made.", ["2d", "palette", "color", "pixel"]),
        ("pixel.draw", "Draw pixel art with ops on layers: pixel, line, rect, ellipse, polygon, fill, gradient (dithered), outline, selout, "
         "shade (auto-shade with a ramp), noise, symmetry, shift, flip, copy, stamp, replace. Colours can be palette indexes ('p3').",
         {"width": "int", "height": "int", "ops": "list[dict]", "palette": "optional list|dict", "source": "optional str",
          "name": "optional str", "save_layers": "optional bool"}, _draw_cap, "Drawing made.", ["2d", "pixel", "draw", "sprite"]),
        ("pixel.sprite_from_3d", "Render an 8 (or 4/1) direction animated pixel-art sprite sheet from a CIE character (rig or new chibi character) "
         "or any 3D model. Same character in every frame. Writes PNG sheet, Aseprite JSON, GIF and palette.",
         {"rig": "optional str", "character": "optional dict", "source": "optional str", "clip": "optional str", "size": "optional int",
          "directions": "optional int", "frames": "optional int", "palette": "optional list", "outline": "optional str", "name": "optional str"},
         _sprite3d_cap, "Sprite sheet made.", ["2d", "pixel", "sprite", "8-direction", "animation"]),
        ("pixel.texture", "Make a seamless texture tile (grass, dirt, sand, stone, water, snow, lava, wood, brick, cobble, metal), pixel or smooth.",
         {"kind": "str", "size": "optional int", "seed": "optional int", "pixel": "optional bool", "colors": "optional int", "base": "optional str"},
         _texture_cap, "Texture made.", ["2d", "texture", "tile", "pixel", "3d"]),
        ("pixel.tileset", "Make a seamless 16-tile dual-grid terrain transition set (upper terrain over lower) with mapping JSON and a demo map.",
         {"upper": "str", "lower": "str", "size": "optional int", "seed": "optional int", "roughness": "optional float", "name": "optional str"},
         _tileset_cap, "Tileset made.", ["2d", "tileset", "autotile", "pixel"]),
        ("pixel.sheet", "Pack frame images into a sprite sheet (bottom-aligned cells) with Aseprite-style JSON and a GIF preview.",
         {"frames": "list[str]", "columns": "optional int", "padding": "optional int", "trim": "optional bool", "duration_ms": "optional int", "name": "optional str"},
         _sheet_cap, "Sheet packed.", ["2d", "spritesheet", "atlas"]),
        ("pixel.animate", "Frame-by-frame 2D animation: effects on a sprite (bob, squash, flash, blink_out, sway, shake, spin) or per-frame draw ops. "
         "Writes sheet, JSON timeline, GIF, onion-skin previews.",
         {"source": "optional str", "effect": "optional str", "frames": "optional int", "base_ops": "optional list", "frame_ops": "optional list[list]",
          "width": "optional int", "height": "optional int", "durations": "optional list[int]", "name": "optional str"},
         _animate_cap, "Animation made.", ["2d", "animation", "gif", "pixel"]),
        ("pixel.ui_kit", "Make a pixel UI kit: 9-slice panel, buttons (normal/hover/pressed), slot, bar, with slice metadata for Godot/Roblox/CSS.",
         {"base": "optional str", "accent": "optional str", "size": "optional int", "name": "optional str"}, _ui_cap, "UI kit made.",
         ["2d", "ui", "9-slice", "pixel"]),
        ("image.edit", "Edit any image into a new file: resize, crop, trim, pad, flip, rotate, adjust (OKLCH lightness/chroma/hue/contrast), blur, "
         "sharpen, remove_background, quantize, pixelate, outline, drop_shadow, glow, compose, mask_circle.",
         {"source": "str", "ops": "list[dict]", "name": "optional str", "palette": "optional list", "pixel_art": "optional bool"}, _edit_cap, "Image edited.",
         ["2d", "image", "edit", "background"]),
        ("image.vector", "Create vector art (rect, circle, ellipse, polygon, path, line, text; flat or linear-gradient fills; strokes): "
         "writes an SVG and an anti-aliased PNG.",
         {"shapes": "list[dict]", "width": "int", "height": "int", "scale": "optional int", "name": "optional str"}, _vector_cap,
         "Vector art made.", ["2d", "svg", "vector", "icon", "ui"]),
    ]
    for cid, summary, schema, fn, done, tags in caps:
        reg.register(CapabilitySpec(id=cid, area="2d", summary=summary, input_schema=schema, permission="read",
                                    handler=wrap(fn, done), tags=tags))
