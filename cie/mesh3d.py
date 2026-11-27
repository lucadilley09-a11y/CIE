"""Native 3D modelling for CIE (Phase 3B). Pure numpy + Pillow: no Blender needed.

A Mesh is a triangle list with per-face materials and optional per-corner UVs.
Conventions match glTF and Roblox: Y is up, -Z is forward, 1 unit = 1 metre,
triangles wind counter-clockwise when seen from outside.

What is here:
  primitives  box, rounded box, plane, wedge, cylinder, cone, uv sphere,
              icosphere, torus, capsule
  edits       transform, merge, weld, flip, orient outward, extrude/inset,
              subdivide (Loop smooth or flat), mirror, decimate, taper, twist,
              bend-free noise displacement
  UVs         box projection, chart unwrap with packing, stretch measurement
  materials   PBR material library and plausibility checks, baked colour atlas
  validation  holes, non-manifold edges, winding, inverted normals, degenerate
              and sliver triangles, duplicate faces, loose vertices, scale
  file I/O    OBJ+MTL, STL, glTF/GLB (read and write, incl. skins/animation
              written by cie.rig)
  rendering   software rasteriser for preview sheets (back faces show pink,
              so holes and flipped faces are visible)

Not here (honest limits): boolean CSG (combine overlapping parts instead, or
use Blender), true edge bevels on arbitrary meshes (use rounded_box / inset),
sculpting brushes.
"""

from __future__ import annotations

import base64
import json
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


def parse_color(value: Any, default: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)) -> list[float]:
    """'#rrggbb', '#rrggbbaa', [r,g,b(,a)] in 0-1 or 0-255 -> sRGB floats 0-1."""
    if value is None:
        return list(default)
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) in (3, 4):
            s = "".join(ch * 2 for ch in s)
        if len(s) not in (6, 8):
            raise ValueError(f"Bad colour '{value}'.")
        vals = [int(s[i:i + 2], 16) / 255.0 for i in range(0, len(s), 2)]
    else:
        vals = [float(v) for v in value]
        if any(v > 1.0 for v in vals):
            vals = [v / 255.0 for v in vals]
    if len(vals) == 3:
        vals.append(1.0)
    return [min(1.0, max(0.0, v)) for v in vals[:4]]


def srgb_to_linear(c: np.ndarray | float) -> np.ndarray:
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c: np.ndarray | float) -> np.ndarray:
    c = np.clip(np.asarray(c, dtype=np.float64), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def to_hex(rgba: Iterable[float]) -> str:
    vals = list(rgba)
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in vals[:3])


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

# sRGB base colours, PBR metallic/roughness. Values follow common physically based charts.
MATERIAL_LIBRARY: dict[str, dict[str, Any]] = {
    "default": {"base_color": "#c8c8c8", "metallic": 0.0, "roughness": 0.6},
    "wood": {"base_color": "#9a6b3f", "metallic": 0.0, "roughness": 0.75, "pattern": "wood"},
    "dark_wood": {"base_color": "#5e3d22", "metallic": 0.0, "roughness": 0.7, "pattern": "wood"},
    "planks": {"base_color": "#a87a4c", "metallic": 0.0, "roughness": 0.8, "pattern": "planks"},
    "stone": {"base_color": "#8a8a86", "metallic": 0.0, "roughness": 0.9, "pattern": "stone"},
    "dark_stone": {"base_color": "#585a5e", "metallic": 0.0, "roughness": 0.9, "pattern": "stone"},
    "brick": {"base_color": "#9c4a36", "metallic": 0.0, "roughness": 0.85, "pattern": "brick"},
    "dirt": {"base_color": "#6b4a2f", "metallic": 0.0, "roughness": 0.95, "pattern": "noise"},
    "grass": {"base_color": "#5f9e3a", "metallic": 0.0, "roughness": 0.85, "pattern": "noise"},
    "leaves": {"base_color": "#3f8a3a", "metallic": 0.0, "roughness": 0.8, "pattern": "noise"},
    "pine": {"base_color": "#2f6b45", "metallic": 0.0, "roughness": 0.8, "pattern": "noise"},
    "sand": {"base_color": "#d9c08a", "metallic": 0.0, "roughness": 0.9, "pattern": "noise"},
    "snow": {"base_color": "#eef2f6", "metallic": 0.0, "roughness": 0.7},
    "ice": {"base_color": "#bfe3f2", "metallic": 0.0, "roughness": 0.15},
    "water": {"base_color": "#3d7fb8", "metallic": 0.0, "roughness": 0.1, "alpha": 0.8},
    "glass": {"base_color": "#d8ecf2", "metallic": 0.0, "roughness": 0.05, "alpha": 0.35},
    "iron": {"base_color": "#c4c7c7", "metallic": 1.0, "roughness": 0.45},
    "steel": {"base_color": "#d6d9db", "metallic": 1.0, "roughness": 0.3},
    "gold": {"base_color": "#f5c75a", "metallic": 1.0, "roughness": 0.3},
    "copper": {"base_color": "#e6a07a", "metallic": 1.0, "roughness": 0.35},
    "rust": {"base_color": "#8b4a2b", "metallic": 0.0, "roughness": 0.9, "pattern": "noise"},
    "plastic": {"base_color": "#d64545", "metallic": 0.0, "roughness": 0.4},
    "rubber": {"base_color": "#2c2c2e", "metallic": 0.0, "roughness": 0.9},
    "cloth": {"base_color": "#4a6fa5", "metallic": 0.0, "roughness": 0.95, "pattern": "noise"},
    "leather": {"base_color": "#7a4b2a", "metallic": 0.0, "roughness": 0.7},
    "skin": {"base_color": "#e0b18f", "metallic": 0.0, "roughness": 0.6},
    "bone": {"base_color": "#e6dcc3", "metallic": 0.0, "roughness": 0.7},
    "lava": {"base_color": "#ff6a1a", "metallic": 0.0, "roughness": 0.6, "emissive": "#ff5a10"},
    "neon": {"base_color": "#40f0ff", "metallic": 0.0, "roughness": 0.3, "emissive": "#40f0ff"},
}


def make_material(name: str, spec: dict[str, Any] | str | None = None) -> dict[str, Any]:
    """Build a material from a library preset and/or explicit values."""
    if isinstance(spec, str):
        spec = {"preset": spec} if spec in MATERIAL_LIBRARY else {"base_color": spec}
    spec = dict(spec or {})
    preset = spec.get("preset") or (name if name in MATERIAL_LIBRARY else "default")
    if preset not in MATERIAL_LIBRARY:
        raise ValueError(f"Unknown material preset '{preset}'. Choose from: {', '.join(sorted(MATERIAL_LIBRARY))}.")
    base = dict(MATERIAL_LIBRARY[preset])
    base.update({k: v for k, v in spec.items() if k != "preset"})
    color = parse_color(base.get("base_color"))
    if "alpha" in base:
        color[3] = float(base["alpha"])
    mat = {
        "name": name,
        "base_color": color,
        "metallic": float(base.get("metallic", 0.0)),
        "roughness": float(base.get("roughness", 0.6)),
        "emissive": parse_color(base["emissive"])[:3] if base.get("emissive") else [0.0, 0.0, 0.0],
        "pattern": base.get("pattern", ""),
        "double_sided": bool(base.get("double_sided", False)),
    }
    return mat


def check_material(mat: dict[str, Any]) -> list[dict[str, Any]]:
    """Physically based plausibility checks (values a renderer will actually make look right)."""
    issues: list[dict[str, Any]] = []
    name = mat.get("name", "?")
    r, g, b, a = mat["base_color"]
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b  # sRGB-space luminance
    m, rough = mat["metallic"], mat["roughness"]
    if 0.15 < m < 0.85:
        issues.append({"severity": "warn", "message": f"Material '{name}': metallic {m:.2f} is between metal and non-metal; real surfaces are ~0 or ~1."})
    if m >= 0.85 and lum < 0.45:
        issues.append({"severity": "warn", "message": f"Material '{name}': metals reflect 50-100% of light; base colour is too dark (lum {lum:.2f})."})
    if m < 0.15 and lum < 0.02:
        issues.append({"severity": "warn", "message": f"Material '{name}': pure black albedo looks flat; use at least ~#101010."})
    if m < 0.15 and lum > 0.95:
        issues.append({"severity": "warn", "message": f"Material '{name}': pure white albedo is brighter than snow; keep it under ~#f2f2f2."})
    if not 0.02 <= rough <= 1.0:
        issues.append({"severity": "warn", "message": f"Material '{name}': roughness {rough:.2f} outside 0.02-1."})
    if a < 1.0 and a < 0.05:
        issues.append({"severity": "warn", "message": f"Material '{name}': alpha {a:.2f} is nearly invisible."})
    return issues


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------


@dataclass
class Mesh:
    V: np.ndarray
    F: np.ndarray
    face_mat: np.ndarray
    materials: list[dict[str, Any]] = field(default_factory=lambda: [make_material("default")])
    UV: np.ndarray | None = None  # (m, 3, 2) per corner
    name: str = "mesh"
    smooth: bool = False
    joints: np.ndarray | None = None   # (n, 4) int, filled by cie.rig
    weights: np.ndarray | None = None  # (n, 4) float

    def copy(self) -> "Mesh":
        return Mesh(
            self.V.copy(), self.F.copy(), self.face_mat.copy(), [dict(m) for m in self.materials],
            None if self.UV is None else self.UV.copy(), self.name, self.smooth,
            None if self.joints is None else self.joints.copy(),
            None if self.weights is None else self.weights.copy(),
        )

    @property
    def face_count(self) -> int:
        return int(len(self.F))

    def face_normals(self, normalize: bool = True) -> np.ndarray:
        if not len(self.F):
            return np.zeros((0, 3))
        a, b, c = self.V[self.F[:, 0]], self.V[self.F[:, 1]], self.V[self.F[:, 2]]
        n = np.cross(b - a, c - a)
        if normalize:
            ln = np.linalg.norm(n, axis=1, keepdims=True)
            n = n / np.where(ln < 1e-20, 1.0, ln)
        return n

    def face_areas(self) -> np.ndarray:
        return 0.5 * np.linalg.norm(self.face_normals(normalize=False), axis=1)

    def vertex_normals(self) -> np.ndarray:
        fn = self.face_normals(normalize=False)  # area weighted
        vn = np.zeros_like(self.V)
        for k in range(3):
            np.add.at(vn, self.F[:, k], fn)
        ln = np.linalg.norm(vn, axis=1, keepdims=True)
        return vn / np.where(ln < 1e-20, 1.0, ln)

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if not len(self.V):
            return np.zeros(3), np.zeros(3)
        return self.V.min(axis=0), self.V.max(axis=0)

    def volume(self) -> float:
        if not len(self.F):
            return 0.0
        a, b, c = self.V[self.F[:, 0]], self.V[self.F[:, 1]], self.V[self.F[:, 2]]
        return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)

    def material_index(self, mat: dict[str, Any]) -> int:
        for i, m in enumerate(self.materials):
            if m["name"] == mat["name"]:
                return i
        self.materials.append(mat)
        return len(self.materials) - 1


def _mk(V: Any, F: Any, material: dict[str, Any] | None = None, name: str = "mesh") -> Mesh:
    V = np.asarray(V, dtype=np.float64).reshape(-1, 3)
    F = np.asarray(F, dtype=np.int64).reshape(-1, 3)
    mat = material or make_material("default")
    return Mesh(V, F, np.zeros(len(F), dtype=np.int64), [mat], None, name)


def _quads(quads: Iterable[Iterable[int]]) -> list[list[int]]:
    tris: list[list[int]] = []
    for q in quads:
        q = list(q)
        for i in range(1, len(q) - 1):
            tris.append([q[0], q[i], q[i + 1]])
    return tris


def _orient_convex(m: Mesh) -> Mesh:
    """Make every face of a convex solid point away from its centre."""
    centre = m.V.mean(axis=0)
    fc = m.V[m.F].mean(axis=1)
    n = m.face_normals(normalize=False)
    flip = np.einsum("ij,ij->i", n, fc - centre) < 0
    m.F[flip] = m.F[flip][:, ::-1]
    return m


# --- primitives ------------------------------------------------------------


def box(size: Iterable[float] = (1, 1, 1), material: dict[str, Any] | None = None) -> Mesh:
    sx, sy, sz = [float(s) / 2 for s in size]
    V = [[x, y, z] for z in (-sz, sz) for y in (-sy, sy) for x in (-sx, sx)]
    # index = x + 2y + 4z
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return _orient_convex(_mk(V, _quads(quads), material, "box"))


def plane(size: Iterable[float] = (1, 1), divisions: int = 1, material: dict[str, Any] | None = None) -> Mesh:
    sx, sz = [float(s) for s in size]
    d = max(1, int(divisions))
    xs = np.linspace(-sx / 2, sx / 2, d + 1)
    zs = np.linspace(-sz / 2, sz / 2, d + 1)
    V = [[x, 0.0, z] for z in zs for x in xs]
    quads = []
    for j in range(d):
        for i in range(d):
            a = j * (d + 1) + i
            quads.append((a, a + d + 1, a + d + 2, a + 1))  # faces +Y
    return _mk(V, _quads(quads), material, "plane")


def wedge(size: Iterable[float] = (1, 1, 1), material: dict[str, Any] | None = None) -> Mesh:
    """Ramp: full height at -Z, zero height at +Z (like Roblox WedgePart)."""
    sx, sy, sz = [float(s) / 2 for s in size]
    V = [[-sx, -sy, -sz], [sx, -sy, -sz], [sx, -sy, sz], [-sx, -sy, sz], [-sx, sy, -sz], [sx, sy, -sz]]
    faces = [(0, 1, 2, 3), (0, 4, 5, 1), (3, 2, 5, 4), (0, 3, 4), (1, 5, 2)]
    return _orient_convex(_mk(V, _quads(faces), material, "wedge"))


def cylinder(radius: float = 0.5, height: float = 1.0, segments: int = 12, radius_top: float | None = None,
             material: dict[str, Any] | None = None, caps: bool = True) -> Mesh:
    seg = max(3, int(segments))
    rt = radius if radius_top is None else float(radius_top)
    h = float(height) / 2
    ang = np.linspace(0, 2 * np.pi, seg, endpoint=False)
    bottom = [[radius * math.cos(a), -h, radius * math.sin(a)] for a in ang]
    if rt <= 1e-9:
        V = bottom + [[0.0, h, 0.0], [0.0, -h, 0.0]]
        apex, bc = seg, seg + 1
        tris = [[i, (i + 1) % seg, apex] for i in range(seg)]
        if caps:
            tris += [[bc, (i + 1) % seg, i] for i in range(seg)]
        return _orient_convex(_mk(V, tris, material, "cone"))
    top = [[rt * math.cos(a), h, rt * math.sin(a)] for a in ang]
    V = bottom + top + [[0.0, -h, 0.0], [0.0, h, 0.0]]
    quads = [(i, (i + 1) % seg, seg + (i + 1) % seg, seg + i) for i in range(seg)]
    tris = _quads(quads)
    if caps:
        bc, tc = 2 * seg, 2 * seg + 1
        tris += [[bc, i, (i + 1) % seg] for i in range(seg)]
        tris += [[tc, seg + i, seg + (i + 1) % seg] for i in range(seg)]
    m = _mk(V, tris, material, "cylinder")
    return _orient_convex(m) if caps else _orient_open_tube(m)


def _orient_open_tube(m: Mesh) -> Mesh:
    c = m.V.copy()
    c[:, 1] = 0
    fc = c[m.F].mean(axis=1)
    n = m.face_normals(normalize=False)
    flip = np.einsum("ij,ij->i", n, fc) < 0
    m.F[flip] = m.F[flip][:, ::-1]
    return m


def cone(radius: float = 0.5, height: float = 1.0, segments: int = 12, material: dict[str, Any] | None = None) -> Mesh:
    return cylinder(radius, height, segments, 0.0, material)


def uv_sphere(radius: float = 0.5, segments: int = 16, rings: int = 8, material: dict[str, Any] | None = None) -> Mesh:
    seg, rings = max(3, int(segments)), max(2, int(rings))
    V = [[0.0, radius, 0.0]]
    for r in range(1, rings):
        phi = math.pi * r / rings
        y = radius * math.cos(phi)
        rr = radius * math.sin(phi)
        for s in range(seg):
            t = 2 * math.pi * s / seg
            V.append([rr * math.cos(t), y, rr * math.sin(t)])
    V.append([0.0, -radius, 0.0])
    bottom = len(V) - 1
    tris: list[list[int]] = []
    for s in range(seg):
        tris.append([0, 1 + s, 1 + (s + 1) % seg])
    for r in range(rings - 2):
        a0 = 1 + r * seg
        b0 = a0 + seg
        for s in range(seg):
            s2 = (s + 1) % seg
            tris += _quads([(a0 + s, b0 + s, b0 + s2, a0 + s2)])
    last = 1 + (rings - 2) * seg
    for s in range(seg):
        tris.append([bottom, last + (s + 1) % seg, last + s])
    return _orient_convex(_mk(V, tris, material, "sphere"))


def icosphere(radius: float = 0.5, subdivisions: int = 1, material: dict[str, Any] | None = None) -> Mesh:
    t = (1 + 5 ** 0.5) / 2
    V = np.array([[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t],
                  [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]], dtype=np.float64)
    F = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6],
         [7, 1, 8], [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11], [6, 2, 10],
         [8, 6, 7], [9, 8, 1]]
    m = _mk(V, F, material, "icosphere")
    for _ in range(max(0, min(int(subdivisions), 5))):
        m = subdivide(m, smooth=False)
    m.V = m.V / np.linalg.norm(m.V, axis=1, keepdims=True) * radius
    return _orient_convex(m)


def torus(radius: float = 0.5, tube: float = 0.15, segments: int = 16, tube_segments: int = 8,
          material: dict[str, Any] | None = None) -> Mesh:
    seg, ts = max(3, int(segments)), max(3, int(tube_segments))
    V = []
    for i in range(seg):
        u = 2 * math.pi * i / seg
        for j in range(ts):
            v = 2 * math.pi * j / ts
            r = radius + tube * math.cos(v)
            V.append([r * math.cos(u), tube * math.sin(v), r * math.sin(u)])
    quads = []
    for i in range(seg):
        for j in range(ts):
            a = i * ts + j
            b = ((i + 1) % seg) * ts + j
            c = ((i + 1) % seg) * ts + (j + 1) % ts
            d = i * ts + (j + 1) % ts
            quads.append((a, b, c, d))
    m = _mk(V, _quads(quads), material, "torus")
    if m.volume() < 0:
        m.F = m.F[:, ::-1].copy()
    return m


def capsule(radius: float = 0.3, height: float = 1.2, segments: int = 12, rings: int = 4,
            material: dict[str, Any] | None = None) -> Mesh:
    """Cylinder with hemispherical ends; height is the total height."""
    s = uv_sphere(radius, segments, rings * 2, material)
    half = max(0.0, float(height) / 2 - radius)
    s.V[:, 1] = np.where(s.V[:, 1] > 1e-9, s.V[:, 1] + half, np.where(s.V[:, 1] < -1e-9, s.V[:, 1] - half, s.V[:, 1]))
    s.name = "capsule"
    return s


def rounded_box(size: Iterable[float] = (1, 1, 1), radius: float = 0.1, segments: int = 2,
                material: dict[str, Any] | None = None) -> Mesh:
    """Box with rounded/bevelled edges (segments=1 gives a 45 degree chamfer)."""
    size = np.array([float(s) for s in size])
    r = min(float(radius), float(size.min()) / 2 - 1e-6)
    seg = max(1, int(segments))
    s = uv_sphere(1.0, 4 * seg, 2 * seg, material)
    # Snap sphere directions onto a superellipse-like rounded box: inner box + radius * direction.
    dirs = s.V / np.linalg.norm(s.V, axis=1, keepdims=True)
    inner = size / 2 - r
    s.V = np.sign(dirs) * inner + dirs * r
    s.V[np.abs(dirs) < 1e-9] = (dirs * r)[np.abs(dirs) < 1e-9]
    s = weld(s, 1e-7)
    s.name = "rounded_box"
    return _orient_convex(s)


PRIMITIVES = {
    "box": lambda p, mat: box(p.get("size", (1, 1, 1)), mat),
    "rounded_box": lambda p, mat: rounded_box(p.get("size", (1, 1, 1)), float(p.get("radius", 0.1)), int(p.get("segments", 2)), mat),
    "plane": lambda p, mat: plane(p.get("size", (1, 1)), int(p.get("divisions", 1)), mat),
    "wedge": lambda p, mat: wedge(p.get("size", (1, 1, 1)), mat),
    "cylinder": lambda p, mat: cylinder(float(p.get("radius", 0.5)), float(p.get("height", 1)), int(p.get("segments", 12)),
                                        p.get("radius_top"), mat, bool(p.get("caps", True))),
    "cone": lambda p, mat: cone(float(p.get("radius", 0.5)), float(p.get("height", 1)), int(p.get("segments", 12)), mat),
    "sphere": lambda p, mat: uv_sphere(float(p.get("radius", 0.5)), int(p.get("segments", 16)), int(p.get("rings", 8)), mat),
    "icosphere": lambda p, mat: icosphere(float(p.get("radius", 0.5)), int(p.get("subdivisions", 1)), mat),
    "torus": lambda p, mat: torus(float(p.get("radius", 0.5)), float(p.get("tube", 0.15)), int(p.get("segments", 16)),
                                  int(p.get("tube_segments", 8)), mat),
    "capsule": lambda p, mat: capsule(float(p.get("radius", 0.3)), float(p.get("height", 1.2)), int(p.get("segments", 12)),
                                      int(p.get("rings", 4)), mat),
}


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


def rotation_matrix(euler_deg: Iterable[float]) -> np.ndarray:
    """XYZ Euler angles in degrees, applied X then Y then Z (R = Rz @ Ry @ Rx)."""
    rx, ry, rz = [math.radians(float(a)) for a in euler_deg]
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def transform(m: Mesh, translate: Iterable[float] = (0, 0, 0), rotate: Iterable[float] = (0, 0, 0),
              scale: Any = 1.0) -> Mesh:
    out = m.copy()
    s = np.array([float(scale)] * 3) if np.isscalar(scale) else np.array([float(v) for v in scale])
    out.V = (out.V * s) @ rotation_matrix(rotate).T + np.array([float(v) for v in translate])
    if np.prod(s) < 0:  # mirrored: keep faces pointing outward
        out.F = out.F[:, ::-1].copy()
        if out.UV is not None:
            out.UV = out.UV[:, ::-1].copy()
    return out


def merge(meshes: list[Mesh], name: str = "mesh") -> Mesh:
    meshes = [m for m in meshes if m is not None and len(m.F)]
    if not meshes:
        return _mk(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), name=name)
    mats: list[dict[str, Any]] = []
    Vs, Fs, FMs, UVs = [], [], [], []
    any_uv = any(m.UV is not None for m in meshes)
    any_skin = all(m.joints is not None for m in meshes)
    Js, Ws = [], []
    off = 0
    for m in meshes:
        remap = []
        for mat in m.materials:
            idx = next((i for i, x in enumerate(mats) if x["name"] == mat["name"]), None)
            if idx is None:
                mats.append(mat)
                idx = len(mats) - 1
            remap.append(idx)
        Vs.append(m.V)
        Fs.append(m.F + off)
        FMs.append(np.asarray(remap, dtype=np.int64)[m.face_mat])
        UVs.append(m.UV if m.UV is not None else np.zeros((len(m.F), 3, 2)))
        if any_skin:
            Js.append(m.joints)
            Ws.append(m.weights)
        off += len(m.V)
    out = Mesh(np.vstack(Vs), np.vstack(Fs), np.concatenate(FMs), mats, np.vstack(UVs) if any_uv else None, name,
               any(m.smooth for m in meshes))
    if any_skin:
        out.joints, out.weights = np.vstack(Js), np.vstack(Ws)
    return out


def weld(m: Mesh, tol: float = 1e-6) -> Mesh:
    """Merge vertices closer than tol and drop faces that collapse."""
    out = m.copy()
    if not len(out.V):
        return out
    keys = np.round(out.V / max(tol, 1e-12)).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)
    out.V = out.V[first]
    if out.joints is not None:
        out.joints, out.weights = out.joints[first], out.weights[first]
    F = inverse[out.F]
    keep = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    out.F, out.face_mat = F[keep], out.face_mat[keep]
    if out.UV is not None:
        out.UV = out.UV[keep]
    return remove_unused(out)


def remove_unused(m: Mesh) -> Mesh:
    used = np.unique(m.F.reshape(-1))
    if len(used) == len(m.V):
        return m
    remap = -np.ones(len(m.V), dtype=np.int64)
    remap[used] = np.arange(len(used))
    m.V = m.V[used]
    if m.joints is not None:
        m.joints, m.weights = m.joints[used], m.weights[used]
    m.F = remap[m.F]
    return m


def _edge_map(F: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Directed half-edges (a,b) per face corner, their undirected id, and edge (sorted) list."""
    he = np.stack([F, np.roll(F, -1, axis=1)], axis=2).reshape(-1, 2)  # face i corner k -> (F[k], F[k+1])
    und = np.sort(he, axis=1)
    edges, eid = np.unique(und, axis=0, return_inverse=True)
    return he, eid.reshape(-1), edges


def orient_outward(m: Mesh) -> Mesh:
    """Make winding consistent across each connected piece, then point closed pieces outward."""
    out = m.copy()
    n = len(out.F)
    if not n:
        return out
    he, eid, edges = _edge_map(out.F)
    face_of = np.repeat(np.arange(n), 3)
    by_edge: dict[int, list[int]] = {}
    for h, e in enumerate(eid):
        by_edge.setdefault(int(e), []).append(h)
    flipped = np.zeros(n, dtype=bool)
    seen = np.zeros(n, dtype=bool)
    comps: list[list[int]] = []
    for start in range(n):
        if seen[start]:
            continue
        comp = [start]
        seen[start] = True
        stack = [start]
        while stack:
            f = stack.pop()
            for k in range(3):
                h = f * 3 + k
                hs = by_edge[int(eid[h])]
                if len(hs) != 2:
                    continue
                other = hs[0] if hs[1] == h else hs[1]
                g = int(face_of[other])
                if seen[g]:
                    continue
                # same direction on both faces means inconsistent winding (after accounting for flips)
                a1, b1 = he[h] if not flipped[f] else he[h][::-1]
                a2, b2 = he[other]
                flipped[g] = (a1 == a2 and b1 == b2)
                seen[g] = True
                comp.append(g)
                stack.append(g)
        comps.append(comp)
    out.F[flipped] = out.F[flipped][:, ::-1]
    if out.UV is not None:
        out.UV[flipped] = out.UV[flipped][:, ::-1]
    for comp in comps:
        idx = np.array(comp)
        sub = out.F[idx]
        a, b, c = out.V[sub[:, 0]], out.V[sub[:, 1]], out.V[sub[:, 2]]
        vol = np.einsum("ij,ij->i", a, np.cross(b, c)).sum()
        if vol < 0:
            out.F[idx] = out.F[idx][:, ::-1]
            if out.UV is not None:
                out.UV[idx] = out.UV[idx][:, ::-1]
    return out


def flip(m: Mesh) -> Mesh:
    out = m.copy()
    out.F = out.F[:, ::-1].copy()
    if out.UV is not None:
        out.UV = out.UV[:, ::-1].copy()
    return out


def select_faces(m: Mesh, sel: Any) -> np.ndarray:
    """Selection: 'all' | list of face ids | {normal:[x,y,z], threshold} | {material} | {above/below:{axis,value}}."""
    n = len(m.F)
    if sel in (None, "all"):
        return np.ones(n, dtype=bool)
    if isinstance(sel, list):
        mask = np.zeros(n, dtype=bool)
        mask[[int(i) for i in sel if 0 <= int(i) < n]] = True
        return mask
    if not isinstance(sel, dict):
        raise ValueError("Bad face selection.")
    mask = np.ones(n, dtype=bool)
    if "normal" in sel:
        d = np.array([float(v) for v in sel["normal"]])
        d = d / (np.linalg.norm(d) or 1)
        mask &= m.face_normals() @ d >= float(sel.get("threshold", 0.9))
    if "material" in sel:
        names = [mm["name"] for mm in m.materials]
        if sel["material"] not in names:
            raise ValueError(f"No material '{sel['material']}' on this mesh.")
        mask &= m.face_mat == names.index(sel["material"])
    for key, cmp in (("above", np.greater_equal), ("below", np.less_equal)):
        if key in sel:
            ax = "xyz".index(str(sel[key].get("axis", "y")))
            centres = m.V[m.F].mean(axis=1)[:, ax]
            mask &= cmp(centres, float(sel[key].get("value", 0)))
    return mask


def _components(F: np.ndarray, mask: np.ndarray) -> list[np.ndarray]:
    idx = np.nonzero(mask)[0]
    parent = {int(v): int(v) for v in np.unique(F[idx].reshape(-1))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f in idx:
        a, b, c = (int(v) for v in F[f])
        for u, v in ((a, b), (b, c)):
            ru, rv = find(u), find(v)
            if ru != rv:
                parent[ru] = rv
    groups: dict[int, list[int]] = {}
    for f in idx:
        groups.setdefault(find(int(F[f, 0])), []).append(int(f))
    return [np.array(g) for g in groups.values()]


def extrude(m: Mesh, sel: Any = "all", distance: float = 0.1, scale: float = 1.0,
            direction: Iterable[float] | None = None) -> Mesh:
    """Extrude selected faces (each connected region separately) and optionally scale the cap.

    distance 0 with scale < 1 is an inset. Side walls take the region's material."""
    out = m.copy()
    out.UV = None
    mask = select_faces(out, sel)
    if not mask.any():
        raise ValueError("Extrude selection matched no faces.")
    fn = out.face_normals()
    for region in _components(out.F, mask):
        rf = out.F[region]
        verts = np.unique(rf.reshape(-1))
        if direction is not None:
            d = np.array([float(v) for v in direction])
            offs = {int(v): d / (np.linalg.norm(d) or 1) for v in verts}
        else:
            acc = {int(v): np.zeros(3) for v in verts}
            for f in region:
                for v in out.F[f]:
                    acc[int(v)] += fn[f]
            offs = {v: a / (np.linalg.norm(a) or 1) for v, a in acc.items()}
        centre = out.V[verts].mean(axis=0)
        mean_off = np.mean([offs[int(x)] for x in verts], axis=0)
        cap_centre = centre + mean_off * float(distance)
        new_index: dict[int, int] = {}
        new_pos = []
        base = len(out.V)
        for i, v in enumerate(verts):
            v = int(v)
            p = out.V[v] + offs[v] * float(distance)
            new_index[v] = base + i
            new_pos.append(cap_centre + (p - cap_centre) * float(scale))
        # boundary edges of the region (directed as they appear in region faces)
        cnt: dict[tuple[int, int], int] = {}
        directed: list[tuple[int, int, int]] = []
        for f in region:
            a, b, c = (int(x) for x in out.F[f])
            for u, v in ((a, b), (b, c), (c, a)):
                key = (min(u, v), max(u, v))
                cnt[key] = cnt.get(key, 0) + 1
                directed.append((u, v, int(f)))
        side_tris, side_mats = [], []
        for u, v, f in directed:
            if cnt[(min(u, v), max(u, v))] == 1:
                nu, nv = new_index[u], new_index[v]
                side_tris += [[u, v, nv], [u, nv, nu]]
                side_mats += [out.face_mat[f]] * 2
        out.V = np.vstack([out.V, np.array(new_pos)])
        if out.joints is not None:
            out.joints = np.vstack([out.joints, out.joints[verts]])
            out.weights = np.vstack([out.weights, out.weights[verts]])
        out.F[region] = np.vectorize(lambda x: new_index.get(int(x), int(x)))(out.F[region])
        if side_tris:
            out.F = np.vstack([out.F, np.array(side_tris, dtype=np.int64)])
            out.face_mat = np.concatenate([out.face_mat, np.array(side_mats, dtype=np.int64)])
    out = remove_unused(out)
    if float(scale) == 0.0:
        out = weld(out, 1e-9)  # cap collapsed to a point: drop the zero-area cap faces
    return out


def subdivide(m: Mesh, smooth: bool = True, levels: int = 1) -> Mesh:
    """Split every triangle in four. smooth=True uses Loop subdivision (rounds the shape)."""
    out = m
    for _ in range(max(1, min(int(levels), 4))):
        out = _subdivide_once(out, smooth)
    return out


def _subdivide_once(m: Mesh, smooth: bool) -> Mesh:
    V, F = m.V, m.F
    n = len(V)
    he, eid, edges = _edge_map(F)
    ne = len(edges)
    eid = eid.reshape(-1, 3)  # edge id for corner k -> edge (F[k], F[k+1])
    # edge points
    if smooth:
        opp_sum = np.zeros((ne, 3))
        cnt = np.zeros(ne)
        for k in range(3):
            np.add.at(opp_sum, eid[:, k], V[F[:, (k + 2) % 3]])
            np.add.at(cnt, eid[:, k], 1)
        mid = V[edges[:, 0]] + V[edges[:, 1]]
        interior = cnt == 2
        epts = np.where(interior[:, None], 0.375 * mid + 0.125 * opp_sum, 0.5 * mid)
        # vertex points
        nb_sum = np.zeros((n, 3))
        val = np.zeros(n)
        np.add.at(nb_sum, edges[:, 0], V[edges[:, 1]])
        np.add.at(nb_sum, edges[:, 1], V[edges[:, 0]])
        np.add.at(val, edges[:, 0], 1)
        np.add.at(val, edges[:, 1], 1)
        boundary_v = np.zeros(n, dtype=bool)
        boundary_v[edges[~interior].reshape(-1)] = True
        beta = np.where(val > 3, 3.0 / (8.0 * np.maximum(val, 1)), 3.0 / 16.0)
        newV = np.where(val[:, None] > 0, (1 - val[:, None] * beta[:, None]) * V + beta[:, None] * nb_sum, V)
        newV[boundary_v] = V[boundary_v]
    else:
        epts = 0.5 * (V[edges[:, 0]] + V[edges[:, 1]])
        newV = V.copy()
    e = eid + n
    a, b, c = F[:, 0], F[:, 1], F[:, 2]
    ab, bc, ca = e[:, 0], e[:, 1], e[:, 2]
    nf = np.concatenate([np.stack([a, ab, ca], 1), np.stack([ab, b, bc], 1), np.stack([ca, bc, c], 1), np.stack([ab, bc, ca], 1)])
    out = Mesh(np.vstack([newV, epts]), nf, np.tile(m.face_mat, 4), m.materials, None, m.name, m.smooth or smooth)
    if m.UV is not None:
        u0, u1, u2 = m.UV[:, 0], m.UV[:, 1], m.UV[:, 2]
        m01, m12, m20 = (u0 + u1) / 2, (u1 + u2) / 2, (u2 + u0) / 2
        out.UV = np.concatenate([np.stack([u0, m01, m20], 1), np.stack([m01, u1, m12], 1),
                                 np.stack([m20, m12, u2], 1), np.stack([m01, m12, m20], 1)])
    if m.joints is not None:
        # edge vertices take the stronger parent's skin
        pick = edges[:, 0]
        out.joints = np.vstack([m.joints, m.joints[pick]])
        out.weights = np.vstack([m.weights, m.weights[pick]])
    return out


def mirror(m: Mesh, axis: str = "x", weld_seam: bool = True) -> Mesh:
    """Mirror across the plane axis=0 and join the halves. Faces lying on the plane are removed
    (they would end up inside the model)."""
    ax = "xyz".index(axis)
    s = [1.0, 1.0, 1.0]
    s[ax] = -1.0
    out = merge([m, transform(m, scale=s)], m.name)
    if not weld_seam:
        return out
    out = weld(out, 1e-5)
    on_plane = np.all(np.abs(out.V[out.F][:, :, ax]) < 1e-5, axis=1)
    if on_plane.any():
        keep = ~on_plane
        out.F, out.face_mat = out.F[keep], out.face_mat[keep]
        if out.UV is not None:
            out.UV = out.UV[keep]
        out = remove_unused(out)
    return out


def decimate(m: Mesh, target_faces: int = 0, ratio: float = 0.5) -> Mesh:
    """Vertex-clustering decimation: fast, robust, keeps silhouettes (bakes flat look)."""
    target = int(target_faces) if target_faces else int(len(m.F) * float(ratio))
    target = max(4, target)
    if len(m.F) <= target:
        return m.copy()
    lo, hi = 1e-4, float(np.max(m.bounds()[1] - m.bounds()[0]) or 1.0)
    best = m
    for _ in range(18):
        cell = (lo + hi) / 2
        cand = _cluster(m, cell)
        if len(cand.F) > target:
            lo = cell
        else:
            hi = cell
            best = cand
        if abs(len(cand.F) - target) <= max(2, target * 0.03):
            best = cand if len(cand.F) <= target * 1.03 else best
            break
    return best


def _cluster(m: Mesh, cell: float) -> Mesh:
    out = m.copy()
    out.UV = None
    keys = np.floor((out.V - out.V.min(axis=0)) / cell).astype(np.int64)
    _, inv = np.unique(keys, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    k = inv.max() + 1
    sums = np.zeros((k, 3))
    np.add.at(sums, inv, out.V)
    cnt = np.bincount(inv, minlength=k)[:, None]
    out.V = sums / cnt
    F = inv[out.F]
    keep = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    F, fm = F[keep], out.face_mat[keep]
    srt = np.sort(F, axis=1)
    _, uniq = np.unique(srt, axis=0, return_index=True)
    uniq = np.sort(uniq)
    out.F, out.face_mat = F[uniq], fm[uniq]
    if out.joints is not None:
        first = np.zeros(k, dtype=np.int64)
        first[inv[::-1]] = np.arange(len(inv))[::-1]
        out.joints, out.weights = m.joints[first], m.weights[first]
    return remove_unused(out)


def taper(m: Mesh, axis: str = "y", amount: float = 0.5) -> Mesh:
    """Scale cross-sections linearly along an axis: amount 1 shrinks the top to a point."""
    out = m.copy()
    ax = "xyz".index(axis)
    lo, hi = out.V[:, ax].min(), out.V[:, ax].max()
    t = (out.V[:, ax] - lo) / ((hi - lo) or 1)
    f = 1 - float(amount) * t
    centre = out.V.mean(axis=0)
    for o in range(3):
        if o != ax:
            out.V[:, o] = centre[o] + (out.V[:, o] - centre[o]) * f
    return out


def twist(m: Mesh, axis: str = "y", degrees: float = 45.0) -> Mesh:
    out = m.copy()
    ax = "xyz".index(axis)
    o1, o2 = [i for i in range(3) if i != ax]
    lo, hi = out.V[:, ax].min(), out.V[:, ax].max()
    ang = np.radians(float(degrees)) * (out.V[:, ax] - lo) / ((hi - lo) or 1)
    c, s = np.cos(ang), np.sin(ang)
    a, b = out.V[:, o1].copy(), out.V[:, o2].copy()
    out.V[:, o1], out.V[:, o2] = a * c - b * s, a * s + b * c
    return out


def jitter(m: Mesh, amount: float = 0.05, seed: int = 0) -> Mesh:
    """Move each (welded) vertex randomly along its normal - turns spheres into rocks."""
    out = weld(m, 1e-6)
    rng = np.random.default_rng(int(seed))
    n = out.vertex_normals()
    out.V = out.V + n * rng.uniform(-1, 1, (len(out.V), 1)) * float(amount)
    return out


def smooth_laplacian(m: Mesh, iterations: int = 2, factor: float = 0.5) -> Mesh:
    out = weld(m, 1e-6)
    _, _, edges = _edge_map(out.F)
    for _ in range(max(1, int(iterations))):
        s = np.zeros_like(out.V)
        c = np.zeros(len(out.V))
        np.add.at(s, edges[:, 0], out.V[edges[:, 1]])
        np.add.at(s, edges[:, 1], out.V[edges[:, 0]])
        np.add.at(c, edges[:, 0], 1)
        np.add.at(c, edges[:, 1], 1)
        avg = s / np.maximum(c, 1)[:, None]
        out.V = out.V + float(factor) * (avg - out.V)
    return out


def set_material(m: Mesh, material: dict[str, Any], sel: Any = "all") -> Mesh:
    out = m.copy()
    idx = out.material_index(material)
    out.face_mat[select_faces(out, sel)] = idx
    return out


# ---------------------------------------------------------------------------
# UVs
# ---------------------------------------------------------------------------


def uv_box(m: Mesh, scale: float = 1.0) -> Mesh:
    """Box projection: each face is projected along its dominant axis (1 UV unit = 1/scale metres)."""
    out = m.copy()
    n = out.face_normals()
    dom = np.argmax(np.abs(n), axis=1)
    P = out.V[out.F]  # (m,3,3)
    uv = np.zeros((len(out.F), 3, 2))
    for ax in range(3):
        sel = dom == ax
        sign = np.sign(n[sel, ax])[:, None]
        sign[sign == 0] = 1
        Q = P[sel]
        if ax == 0:    # looking at +X, right is -Z
            uv[sel, :, 0], uv[sel, :, 1] = -sign * Q[:, :, 2], Q[:, :, 1]
        elif ax == 1:  # looking down at +Y, right is +X, up is -Z
            uv[sel, :, 0], uv[sel, :, 1] = Q[:, :, 0], -sign * Q[:, :, 2]
        else:          # looking at +Z, right is +X
            uv[sel, :, 0], uv[sel, :, 1] = sign * Q[:, :, 0], Q[:, :, 1]
    out.UV = uv * float(scale)
    return out


def uv_unwrap(m: Mesh, angle_deg: float = 35.0, padding: float = 0.01) -> Mesh:
    """Chart unwrap: group connected faces that face the same way, flatten each chart onto
    its own plane (little stretch), and pack all charts into the 0-1 square without overlap."""
    out = weld(m, 1e-6) if m.joints is None else m.copy()
    nf = len(out.F)
    fn = out.face_normals()
    he, eid, _ = _edge_map(out.F)
    by_edge: dict[int, list[int]] = {}
    for h, e in enumerate(eid):
        by_edge.setdefault(int(e), []).append(h // 3)
    cos_lim = math.cos(math.radians(float(angle_deg)))
    chart = -np.ones(nf, dtype=np.int64)
    charts: list[list[int]] = []
    for seed in np.argsort(-out.face_areas()):
        if chart[seed] >= 0:
            continue
        cid = len(charts)
        ref = fn[seed]
        stack, members = [int(seed)], [int(seed)]
        chart[seed] = cid
        while stack:
            f = stack.pop()
            for k in range(3):
                for g in by_edge[int(eid[f * 3 + k])]:
                    if chart[g] < 0 and fn[g] @ ref >= cos_lim:
                        chart[g] = cid
                        members.append(g)
                        stack.append(g)
        charts.append(members)
    uv = np.zeros((nf, 3, 2))
    rects = []
    for members in charts:
        idx = np.array(members)
        nrm = (fn[idx] * out.face_areas()[idx, None]).sum(axis=0)
        nrm = nrm / (np.linalg.norm(nrm) or 1)
        helper = np.array([0, 1, 0]) if abs(nrm[1]) < 0.9 else np.array([1, 0, 0])
        tu = np.cross(helper, nrm)
        tu /= np.linalg.norm(tu)
        tv = np.cross(nrm, tu)
        P = out.V[out.F[idx]]
        cu, cv = P @ tu, P @ tv
        cu -= cu.min()
        cv -= cv.min()
        uv[idx, :, 0], uv[idx, :, 1] = cu, cv
        rects.append((float(cu.max()), float(cv.max()), idx))
    # shelf packing, tallest first
    order = sorted(range(len(rects)), key=lambda i: -rects[i][1])
    total_area = sum(w * h for w, h, _ in rects) or 1.0
    shelf_w = max(math.sqrt(total_area) * 1.15, max(r[0] for r in rects))
    x = y = row_h = 0.0
    pad = float(padding) * shelf_w
    placed = {}
    for i in order:
        w, h, _ = rects[i]
        if x + w > shelf_w and x > 0:
            x, y, row_h = 0.0, y + row_h + pad, 0.0
        placed[i] = (x, y)
        x += w + pad
        row_h = max(row_h, h)
    extent = max(shelf_w, y + row_h) or 1.0
    for i, (ox, oy) in placed.items():
        idx = rects[i][2]
        uv[idx, :, 0] = (uv[idx, :, 0] + ox) / extent
        uv[idx, :, 1] = (uv[idx, :, 1] + oy) / extent
    out.UV = uv
    out._charts = len(charts)  # type: ignore[attr-defined]
    return out


def uv_stats(m: Mesh) -> dict[str, Any]:
    """Stretch per triangle from the UV->3D Jacobian. 1.0 = no stretch."""
    if m.UV is None:
        return {"has_uv": False}
    P = m.V[m.F]
    e1, e2 = P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
    t1, t2 = m.UV[:, 1] - m.UV[:, 0], m.UV[:, 2] - m.UV[:, 0]
    det = t1[:, 0] * t2[:, 1] - t2[:, 0] * t1[:, 1]
    good = np.abs(det) > 1e-14
    area3 = m.face_areas()
    uv_area = 0.5 * np.abs(det)
    if not good.any():
        return {"has_uv": True, "degenerate_uv_faces": int(len(m.F)), "max_stretch": float("inf")}
    inv = 1.0 / det[good]
    Ss = (e1[good] * t2[good, 1:2] - e2[good] * t1[good, 1:2]) * inv[:, None]
    St = (e2[good] * t1[good, 0:1] - e1[good] * t2[good, 0:1]) * inv[:, None]
    a = np.einsum("ij,ij->i", Ss, Ss)
    b = np.einsum("ij,ij->i", Ss, St)
    c = np.einsum("ij,ij->i", St, St)
    root = np.sqrt(np.maximum((a - c) ** 2 + 4 * b * b, 0))
    smax = np.sqrt(np.maximum(0.5 * (a + c + root), 0))
    smin = np.sqrt(np.maximum(0.5 * (a + c - root), 0))
    aniso = smax / np.maximum(smin, 1e-12)
    w = area3[good] / (area3[good].sum() or 1)
    density = uv_area[good] / np.maximum(area3[good], 1e-12)
    dens_norm = density / (np.sum(density * w) or 1)
    flipped = int(np.sum(det[good] * np.sign(np.einsum("ij,ij->i", m.face_normals()[good], np.cross(e1[good], e2[good]))) < 0))
    return {
        "has_uv": True,
        "degenerate_uv_faces": int((~good).sum()),
        "mean_stretch": round(float(np.sum(aniso * w)), 3),
        "max_stretch": round(float(aniso.max()), 3),
        "texel_density_spread": round(float(np.sqrt(np.sum(w * (dens_norm - 1) ** 2))), 3),
        "mirrored_uv_faces": flipped,
    }


# ---------------------------------------------------------------------------
# Procedural texture patterns (for baked atlases and cie.pixel textures)
# ---------------------------------------------------------------------------


def value_noise(w: int, h: int, scale: float, seed: int = 0, octaves: int = 4, tileable: bool = True) -> np.ndarray:
    rng = np.random.default_rng(int(seed))
    out = np.zeros((h, w))
    amp, total = 1.0, 0.0
    for o in range(max(1, int(octaves))):
        cells = max(2, int(round(scale * (2 ** o))))
        grid = rng.random((cells + 1, cells + 1))
        if tileable:
            grid[-1, :], grid[:, -1] = grid[0, :], grid[:, 0]
        ys = np.linspace(0, cells, h, endpoint=False)
        xs = np.linspace(0, cells, w, endpoint=False)
        yi, xi = ys.astype(int), xs.astype(int)
        fy, fx = ys - yi, xs - xi
        fy, fx = fy * fy * (3 - 2 * fy), fx * fx * (3 - 2 * fx)
        g00 = grid[yi][:, xi]
        g10 = grid[yi][:, xi + 1]
        g01 = grid[yi + 1][:, xi]
        g11 = grid[yi + 1][:, xi + 1]
        top = g00 + (g10 - g00) * fx[None, :]
        bot = g01 + (g11 - g01) * fx[None, :]
        out += amp * (top + (bot - top) * fy[:, None])
        total += amp
        amp *= 0.5
    return out / total


def pattern_texture(kind: str, base_color: Iterable[float], size: int = 64, seed: int = 0) -> np.ndarray:
    """Tileable RGBA texture (uint8) for a material pattern."""
    base = np.array(list(base_color)[:3], dtype=np.float64)
    s = int(size)
    yy, xx = np.mgrid[0:s, 0:s] / s
    n = value_noise(s, s, 4, seed)
    if kind == "wood":
        grain = np.sin((yy * 18 + value_noise(s, s, 2, seed + 1, 3) * 3) * np.pi) * 0.5 + 0.5
        f = 0.82 + 0.18 * grain * (0.7 + 0.3 * n)
    elif kind == "planks":
        plank = (np.floor(yy * 4) % 2) * 0.06
        seam = (np.abs((yy * 4) % 1 - 0.0) < 0.04).astype(float)
        grain = np.sin((xx * 30 + value_noise(s, s, 2, seed + 2, 3) * 4) * np.pi) * 0.5 + 0.5
        f = 0.85 + 0.1 * grain - plank - 0.35 * seam
    elif kind == "brick":
        row = np.floor(yy * 8)
        off = (row % 2) * 0.125
        mortar = ((np.abs(((xx + off) * 4) % 1) < 0.06) | (np.abs((yy * 8) % 1) < 0.12)).astype(float)
        f = (0.85 + 0.25 * n) * (1 - mortar) + 1.35 * mortar
        base = np.where(mortar[..., None] > 0, np.array([0.75, 0.72, 0.68]) / 1.35, base)
    elif kind == "stone":
        cells = value_noise(s, s, 3, seed + 3, 1)
        cracks = np.clip(1 - np.abs(np.gradient(cells)[0]) * s * 0.6, 0, 1)
        f = (0.78 + 0.35 * n) * (0.75 + 0.25 * cracks)
    else:  # noise
        f = 0.85 + 0.3 * (n - 0.5) * 2 * 0.5
    if np.ndim(base) == 1:
        rgb = np.clip(base[None, None, :] * f[..., None], 0, 1)
    else:
        rgb = np.clip(base * f[..., None], 0, 1)
    rgba = np.concatenate([rgb, np.ones((s, s, 1))], axis=2)
    return (rgba * 255 + 0.5).astype(np.uint8)


# ---------------------------------------------------------------------------
# Software rasteriser (preview sheets)
# ---------------------------------------------------------------------------

VIEWS = {
    "front": (0.0, 0.0), "back": (180.0, 0.0), "left": (-90.0, 0.0), "right": (90.0, 0.0),
    "top": (0.0, 89.0), "three_quarter": (35.0, 25.0), "low_angle": (-30.0, -15.0),
}


def _view_matrix(yaw: float, pitch: float) -> np.ndarray:
    y, p = math.radians(yaw), math.radians(pitch)
    Ry = np.array([[math.cos(y), 0, -math.sin(y)], [0, 1, 0], [math.sin(y), 0, math.cos(y)]])
    Rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    return Rx @ Ry


def render(m: Mesh, size: int = 256, yaw: float = 35.0, pitch: float = 25.0, wire: bool = False,
           show_backfaces: bool = True, texture: np.ndarray | None = None, fit_bounds: tuple | None = None,
           bg: tuple[int, int, int, int] = (0, 0, 0, 0)) -> Image.Image:
    """Orthographic, flat-shaded render. Back faces draw pink so holes and flips stand out."""
    size = int(size)
    img = np.zeros((size, size, 4), dtype=np.float64)
    img[:] = np.array(bg) / 255.0
    if not len(m.F):
        return Image.fromarray((img * 255).astype(np.uint8), "RGBA")
    R = _view_matrix(yaw, pitch)
    lo, hi = fit_bounds if fit_bounds is not None else m.bounds()
    centre = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2) or 1.0
    P = (m.V - centre) @ R.T
    scale = size * 0.46 / radius
    sx = size / 2 + P[:, 0] * scale
    sy = size / 2 - P[:, 1] * scale
    sz = P[:, 2]  # +z is toward the camera after the view rotation? camera looks along -z: nearer = larger z
    zbuf = np.full((size, size), -np.inf)
    fn = m.face_normals() @ R.T
    light = np.array([0.45, 0.75, 0.5])
    light /= np.linalg.norm(light)
    mats = m.materials
    base = np.array([mats[i]["base_color"] for i in range(len(mats))])
    lin = srgb_to_linear(base[:, :3])
    emis = np.array([mats[i].get("emissive", [0, 0, 0]) for i in range(len(mats))])
    for f in range(len(m.F)):
        i0, i1, i2 = m.F[f]
        x0, y0, x1, y1, x2, y2 = sx[i0], sy[i0], sx[i1], sy[i1], sx[i2], sy[i2]
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-12:
            continue
        back = fn[f, 2] < 0
        if back and (not show_backfaces or base[int(m.face_mat[f]), 3] < 0.999):
            continue  # transparent materials: their inside is meant to be seen, don't flag it
        minx, maxx = max(int(math.floor(min(x0, x1, x2))), 0), min(int(math.ceil(max(x0, x1, x2))), size - 1)
        miny, maxy = max(int(math.floor(min(y0, y1, y2))), 0), min(int(math.ceil(max(y0, y1, y2))), size - 1)
        if minx > maxx or miny > maxy:
            continue
        ys, xs = np.mgrid[miny:maxy + 1, minx:maxx + 1] + 0.5
        w0 = ((x1 - xs) * (y2 - ys) - (x2 - xs) * (y1 - ys)) / area
        w1 = ((x2 - xs) * (y0 - ys) - (x0 - xs) * (y2 - ys)) / area
        w2 = 1 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        if not inside.any():
            continue
        z = w0 * sz[i0] + w1 * sz[i1] + w2 * sz[i2]
        region = zbuf[miny:maxy + 1, minx:maxx + 1]
        upd = inside & (z > region)
        if not upd.any():
            continue
        region[upd] = z[upd]
        mi = int(m.face_mat[f])
        if back:
            col = np.array([1.0, 0.25, 0.6])
            alpha = 1.0
            shade = np.broadcast_to(col, upd.shape + (3,))[upd]
        else:
            nrm = fn[f]
            diff = max(0.0, float(nrm @ light))
            k = 0.32 + 0.68 * diff + 0.12 * max(0.0, nrm[2])
            if texture is not None and m.UV is not None:
                uv = m.UV[f]
                u = (w0 * uv[0, 0] + w1 * uv[1, 0] + w2 * uv[2, 0])[upd]
                v = (w0 * uv[0, 1] + w1 * uv[1, 1] + w2 * uv[2, 1])[upd]
                th, tw = texture.shape[:2]
                tx = np.clip((u % 1.0) * tw, 0, tw - 1).astype(int)
                ty = np.clip((1 - v % 1.0) * th, 0, th - 1).astype(int)
                tex_lin = srgb_to_linear(texture[ty, tx, :3] / 255.0)
                shade = linear_to_srgb(tex_lin * k + emis[mi])
            else:
                shade = np.broadcast_to(linear_to_srgb(lin[mi] * k + emis[mi]), upd.shape + (3,))[upd]
            alpha = float(base[mi, 3])
        sub = img[miny:maxy + 1, minx:maxx + 1]
        if alpha >= 0.999:
            sub[upd, :3] = shade
            sub[upd, 3] = 1.0
        else:
            sub[upd, :3] = sub[upd, :3] * (1 - alpha) + shade * alpha
            sub[upd, 3] = np.maximum(sub[upd, 3], alpha)
    out = Image.fromarray((np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")
    if wire:
        draw = ImageDraw.Draw(out)
        _, _, edges = _edge_map(m.F)
        for a, b in edges:
            draw.line([(sx[a], sy[a]), (sx[b], sy[b])], fill=(20, 22, 26, 150), width=1)
    return out


def highlight_edges(img: Image.Image, m: Mesh, edges: np.ndarray, yaw: float, pitch: float,
                    color: tuple[int, int, int, int] = (255, 60, 60, 255)) -> Image.Image:
    if not len(edges):
        return img
    size = img.size[0]
    R = _view_matrix(yaw, pitch)
    lo, hi = m.bounds()
    centre = (lo + hi) / 2
    radius = float(np.linalg.norm(hi - lo) / 2) or 1.0
    P = (m.V - centre) @ R.T
    scale = size * 0.46 / radius
    sx, sy = size / 2 + P[:, 0] * scale, size / 2 - P[:, 1] * scale
    out = img.copy()
    d = ImageDraw.Draw(out)
    for a, b in edges[:500]:
        d.line([(sx[a], sy[a]), (sx[b], sy[b])], fill=color, width=2)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(m: Mesh, target: str = "generic") -> dict[str, Any]:
    """Geometry checks that matter for game engines. target: generic | roblox | minecraft | web."""
    issues: list[dict[str, Any]] = []
    stats: dict[str, Any] = {"vertices": int(len(m.V)), "triangles": int(len(m.F)), "materials": len(m.materials)}
    if not len(m.F):
        return {"stats": stats, "issues": [{"severity": "error", "message": "Mesh has no faces."}], "verdict": "fail",
                "boundary": np.zeros((0, 2), dtype=np.int64), "_split": m}
    w = weld(m, 1e-6)
    he, eid, edges = _edge_map(w.F)
    counts = np.bincount(eid, minlength=len(edges))
    # Separate parts that merely touch (a rail meeting a post) before judging topology:
    # faces are joined into shells only across edges that exactly two faces share.
    shells = _shells(w.F, eid, counts)
    touching = int(np.sum(counts > 2))
    split = Mesh(w.V, w.F.copy(), w.face_mat, w.materials)
    key = shells[:, None] * (len(w.V) + 1) + w.F
    _, inv = np.unique(key.reshape(-1), return_inverse=True)
    split.F = inv.reshape(-1, 3)
    split.V = np.zeros((inv.max() + 1, 3))
    split.V[split.F.reshape(-1)] = w.V[w.F.reshape(-1)]
    he, eid, edges = _edge_map(split.F)
    counts = np.bincount(eid, minlength=len(edges))
    boundary = edges[counts == 1]
    nonmanifold = edges[counts > 2]
    _, dcount = np.unique(he, axis=0, return_counts=True)
    inconsistent = int(np.sum(dcount > 1))
    w_closed_volume = split.volume()
    w_view = split
    areas = w.face_areas()
    lo, hi = w.bounds()
    dims = hi - lo
    diag = float(np.linalg.norm(dims)) or 1.0
    degenerate = int(np.sum(areas < (diag * 1e-5) ** 2))
    # sliver triangles: min angle < 2 degrees
    P = w.V[w.F]
    def ang(a, b, c):
        u, v = b - a, c - a
        cosv = np.einsum("ij,ij->i", u, v) / np.maximum(np.linalg.norm(u, axis=1) * np.linalg.norm(v, axis=1), 1e-20)
        return np.degrees(np.arccos(np.clip(cosv, -1, 1)))
    angs = np.stack([ang(P[:, 0], P[:, 1], P[:, 2]), ang(P[:, 1], P[:, 2], P[:, 0]), ang(P[:, 2], P[:, 0], P[:, 1])], 1)
    min_ang, max_ang = angs.min(axis=1), angs.max(axis=1)
    # Long thin quads (tall cylinders) are harmless; flat 'cap' triangles and needles are not.
    slivers = int(np.sum(((max_ang > 170.0) | (min_ang < 0.5)) & (areas >= (diag * 1e-5) ** 2)))
    srt = np.sort(w.F, axis=1)
    _, fc = np.unique(srt, axis=0, return_counts=True)
    dup_faces = int(np.sum(fc - 1))
    loose = int(len(m.V) - len(np.unique(m.F.reshape(-1))))
    closed = len(boundary) == 0 and len(nonmanifold) == 0
    vol = w_closed_volume
    holes = _count_loops(boundary)
    stats.update({
        "welded_vertices": int(len(w.V)), "edges": int(len(edges)), "boundary_edges": int(len(boundary)),
        "holes": holes, "non_manifold_edges": int(len(nonmanifold)), "parts": int(len(np.unique(shells))),
        "touching_part_edges": touching, "inconsistent_winding_edges": inconsistent,
        "degenerate_faces": degenerate, "sliver_faces": slivers, "duplicate_faces": dup_faces,
        "loose_vertices": loose, "watertight": bool(closed), "volume_m3": round(vol, 5) if closed else None,
        "size_m": [round(float(x), 4) for x in dims], "surface_m2": round(float(areas.sum()), 4),
        "min_triangle_angle_deg": round(float(min_ang.min()), 2),
    })
    if holes:
        issues.append({"severity": "warn", "message": f"{holes} open hole(s) ({len(boundary)} boundary edges). Fine for planes/foliage cards; a solid prop should be closed."})
    if len(nonmanifold):
        issues.append({"severity": "error", "message": f"{len(nonmanifold)} non-manifold edges (shared by 3+ faces); engines and physics break on these."})
    if inconsistent:
        issues.append({"severity": "error", "message": f"{inconsistent} edges with inconsistent winding; run orient_outward."})
    if closed and vol < 0:
        issues.append({"severity": "error", "message": "Normals point inward (negative volume); run orient_outward or flip."})
    if degenerate:
        issues.append({"severity": "warn", "message": f"{degenerate} zero-area faces; run weld."})
    if slivers:
        issues.append({"severity": "warn", "message": f"{slivers} sliver triangles (a near-180 or near-0 degree corner) can shade badly."})
    if dup_faces:
        issues.append({"severity": "error", "message": f"{dup_faces} duplicate faces (z-fighting)."})
    if loose:
        issues.append({"severity": "warn", "message": f"{loose} loose vertices not used by any face."})
    biggest = float(dims.max())
    smallest = float(dims[dims > 0].min()) if (dims > 0).any() else 0.0
    scale_rules = {"generic": (0.01, 500.0), "roblox": (0.05, 2048.0 * 0.28), "minecraft": (1 / 16, 64.0), "web": (0.01, 1000.0)}
    lo_ok, hi_ok = scale_rules.get(target, scale_rules["generic"])
    if biggest < lo_ok:
        issues.append({"severity": "warn", "message": f"Model is {biggest:.4f} m across; likely the wrong unit (CIE uses metres)."})
    if biggest > hi_ok:
        issues.append({"severity": "warn", "message": f"Model is {biggest:.1f} m across; check the unit scale."})
    if smallest and biggest / smallest > 2000:
        issues.append({"severity": "warn", "message": "Very thin along one axis; check it isn't flattened by accident."})
    budget = {"roblox": 20000, "web": 50000, "minecraft": 5000}.get(target)
    if budget and len(m.F) > budget:
        issues.append({"severity": "warn", "message": f"{len(m.F)} triangles is above the {target} budget of {budget}; decimate."})
    if target == "roblox" and len(m.F) > 20000:
        issues.append({"severity": "error", "message": "Roblox MeshParts are limited to 20,000 triangles."})
    for mat in m.materials:
        issues.extend(check_material(mat))
    uv = uv_stats(m)
    stats["uv"] = uv
    if uv.get("has_uv"):
        if uv.get("degenerate_uv_faces"):
            issues.append({"severity": "warn", "message": f"{uv['degenerate_uv_faces']} faces have zero UV area (texture will smear)."})
        if uv.get("max_stretch", 1) > 2.5:
            issues.append({"severity": "warn", "message": f"UV stretch up to {uv['max_stretch']}x; re-unwrap with uv=unwrap."})
    from .studio import verdict
    return {"stats": stats, "issues": issues, "verdict": verdict(issues), "boundary": boundary, "_split": w_view}


def _shells(F: np.ndarray, eid: np.ndarray, counts: np.ndarray) -> np.ndarray:
    n = len(F)
    parent = np.arange(n)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    order = np.argsort(eid, kind="stable")
    se = eid[order]
    for i in range(len(se) - 1):
        if se[i] == se[i + 1] and counts[se[i]] == 2:
            a, b = find(int(order[i] // 3)), find(int(order[i + 1] // 3))
            if a != b:
                parent[a] = b
    return np.array([find(i) for i in range(n)])


def _count_loops(boundary: np.ndarray) -> int:
    if not len(boundary):
        return 0
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in boundary:
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    return len({find(int(v)) for v in boundary.reshape(-1)})


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _unroll(m: Mesh) -> dict[str, np.ndarray]:
    """Per-corner vertex streams (flat or smooth normals)."""
    corners = m.F.reshape(-1)
    pos = m.V[corners]
    if m.smooth:
        nrm = weld(m, 1e-6)
        # map each original vertex to welded normal by position
        keys_w = {tuple(np.round(v, 6)): i for i, v in enumerate(nrm.V)}
        vn = nrm.vertex_normals()
        nrm_c = np.array([vn[keys_w.get(tuple(np.round(p, 6)), 0)] for p in pos])
    else:
        nrm_c = np.repeat(m.face_normals(), 3, axis=0)
    out = {"pos": pos.astype(np.float32), "nrm": nrm_c.astype(np.float32), "mat": np.repeat(m.face_mat, 3)}
    if m.UV is not None:
        out["uv"] = m.UV.reshape(-1, 2).astype(np.float32)
    if m.joints is not None:
        out["joints"] = m.joints[corners].astype(np.uint16)
        out["weights"] = m.weights[corners].astype(np.float32)
    return out


def write_obj(m: Mesh, path: Path, texture: np.ndarray | None = None) -> list[Path]:
    path = Path(path)
    mtl = path.with_suffix(".mtl")
    files = [path, mtl]
    tex_name = None
    if texture is not None:
        tex_path = path.with_name(f"{path.stem}_tex.png")
        Image.fromarray(texture, "RGBA").save(tex_path)
        tex_name = tex_path.name
        files.append(tex_path)
    with mtl.open("w", encoding="utf-8", newline="\n") as fh:
        for mat in m.materials:
            r, g, b, a = mat["base_color"]
            fh.write(f"newmtl {mat['name']}\nKd {r:.4f} {g:.4f} {b:.4f}\nKa 0 0 0\nKs {0.5 * mat['metallic']:.3f} {0.5 * mat['metallic']:.3f} {0.5 * mat['metallic']:.3f}\n")
            fh.write(f"Ns {max(1.0, (1 - mat['roughness']) * 200):.1f}\nd {a:.3f}\nillum 2\n")
            if tex_name:
                fh.write(f"map_Kd {tex_name}\n")
            fh.write("\n")
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# CIE studio mesh '{m.name}'\nmtllib {mtl.name}\no {m.name}\n")
        for v in m.V:
            fh.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        if m.UV is not None:
            for uv in m.UV.reshape(-1, 2):
                fh.write(f"vt {uv[0]:.6f} {uv[1]:.6f}\n")
        fn = m.face_normals()
        for n in fn:
            fh.write(f"vn {n[0]:.5f} {n[1]:.5f} {n[2]:.5f}\n")
        fh.write("s 1\n" if m.smooth else "s off\n")
        cur = -1
        for i, f in enumerate(m.F):
            if m.face_mat[i] != cur:
                cur = int(m.face_mat[i])
                fh.write(f"usemtl {m.materials[cur]['name']}\n")
            if m.UV is not None:
                t = i * 3 + 1
                fh.write(f"f {f[0] + 1}/{t}/{i + 1} {f[1] + 1}/{t + 1}/{i + 1} {f[2] + 1}/{t + 2}/{i + 1}\n")
            else:
                fh.write(f"f {f[0] + 1}//{i + 1} {f[1] + 1}//{i + 1} {f[2] + 1}//{i + 1}\n")
    return files


def read_obj(path: Path) -> Mesh:
    path = Path(path)
    V, VT, F, FT, FM = [], [], [], [], []
    mats: list[dict[str, Any]] = []
    mat_lookup: dict[str, dict[str, Any]] = {}
    cur = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "v" and len(parts) >= 4:
            V.append([float(x) for x in parts[1:4]])
        elif tag == "vt" and len(parts) >= 3:
            VT.append([float(parts[1]), float(parts[2])])
        elif tag == "mtllib":
            mtl = path.with_name(" ".join(parts[1:]))
            if mtl.exists():
                mat_lookup.update(_read_mtl(mtl))
        elif tag == "usemtl":
            name = " ".join(parts[1:]) or "default"
            existing = [i for i, m in enumerate(mats) if m["name"] == name]
            if existing:
                cur = existing[0]
            else:
                mats.append(mat_lookup.get(name, make_material(name if name in MATERIAL_LIBRARY else "default") | {"name": name}))
                cur = len(mats) - 1
        elif tag == "f" and len(parts) >= 4:
            vs, ts = [], []
            for tok in parts[1:]:
                bits = tok.split("/")
                vi = int(bits[0])
                vs.append(vi - 1 if vi > 0 else len(V) + vi)
                if len(bits) > 1 and bits[1]:
                    ti = int(bits[1])
                    ts.append(ti - 1 if ti > 0 else len(VT) + ti)
            for i in range(1, len(vs) - 1):
                F.append([vs[0], vs[i], vs[i + 1]])
                FT.append([ts[0], ts[i], ts[i + 1]] if len(ts) == len(vs) else None)
                FM.append(cur)
    if not mats:
        mats = [make_material("default")]
    m = Mesh(np.array(V, dtype=np.float64).reshape(-1, 3), np.array(F, dtype=np.int64).reshape(-1, 3),
             np.array(FM, dtype=np.int64), mats, None, path.stem)
    if VT and FT and all(t is not None for t in FT):
        m.UV = np.array(VT)[np.array(FT)]
    return m


def _read_mtl(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    cur: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.strip().split()
        if not p:
            continue
        if p[0] == "newmtl":
            cur = make_material(" ".join(p[1:]) or "m")
            cur["name"] = " ".join(p[1:]) or "m"
            out[cur["name"]] = cur
        elif cur is not None and p[0] == "Kd" and len(p) >= 4:
            cur["base_color"][:3] = [float(x) for x in p[1:4]]
        elif cur is not None and p[0] == "d" and len(p) >= 2:
            cur["base_color"][3] = float(p[1])
    return out


def write_stl(m: Mesh, path: Path) -> list[Path]:
    path = Path(path)
    fn = m.face_normals().astype(np.float32)
    P = m.V[m.F].astype(np.float32)
    rec = np.zeros(len(m.F), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["n"], rec["v"] = fn, P
    with path.open("wb") as fh:
        fh.write(b"CIE studio STL".ljust(80, b" "))
        fh.write(struct.pack("<I", len(m.F)))
        fh.write(rec.tobytes())
    return [path]


def read_stl(path: Path) -> Mesh:
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + n * 50 == len(data):
            rec = np.frombuffer(data[84:], dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            P = rec["v"].astype(np.float64).reshape(-1, 3)
            return weld(_mk(P, np.arange(len(P)).reshape(-1, 3), name=Path(path).stem), 1e-7)
    pts = [[float(x) for x in ln.split()[1:4]] for ln in data.decode("utf-8", "replace").splitlines() if ln.strip().startswith("vertex")]
    P = np.array(pts, dtype=np.float64).reshape(-1, 3)
    return weld(_mk(P, np.arange(len(P)).reshape(-1, 3), name=Path(path).stem), 1e-7)


class GLTFBuilder:
    """Minimal glTF 2.0 writer: meshes per material, textures, skins and animations."""

    def __init__(self) -> None:
        self.bin = bytearray()
        self.doc: dict[str, Any] = {"asset": {"version": "2.0", "generator": "CIE studio"}, "scene": 0,
                                    "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [],
                                    "accessors": [], "bufferViews": [], "buffers": []}

    def view(self, data: bytes, target: int | None = None) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        off = len(self.bin)
        self.bin.extend(data)
        bv: dict[str, Any] = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            bv["target"] = target
        self.doc["bufferViews"].append(bv)
        return len(self.doc["bufferViews"]) - 1

    def accessor(self, arr: np.ndarray, kind: str, target: int | None = None, minmax: bool = False,
                 normalized: bool = False) -> int:
        comp = {np.dtype(np.float32): 5126, np.dtype(np.uint16): 5123, np.dtype(np.uint32): 5125, np.dtype(np.uint8): 5121}[arr.dtype]
        bv = self.view(np.ascontiguousarray(arr).tobytes(), target)
        acc: dict[str, Any] = {"bufferView": bv, "componentType": comp, "count": int(arr.shape[0]), "type": kind}
        if normalized:
            acc["normalized"] = True
        if minmax:
            flat = arr.reshape(arr.shape[0], -1)
            acc["min"] = [float(x) for x in flat.min(axis=0)]
            acc["max"] = [float(x) for x in flat.max(axis=0)]
        self.doc["accessors"].append(acc)
        return len(self.doc["accessors"]) - 1

    def material(self, mat: dict[str, Any], texture_png: bytes | None = None) -> int:
        lin = [float(x) for x in srgb_to_linear(np.array(mat["base_color"][:3]))] + [float(mat["base_color"][3])]
        pbr: dict[str, Any] = {"baseColorFactor": lin, "metallicFactor": mat["metallic"], "roughnessFactor": mat["roughness"]}
        out: dict[str, Any] = {"name": mat["name"], "pbrMetallicRoughness": pbr}
        if texture_png is not None:
            self.doc.setdefault("images", [])
            self.doc.setdefault("textures", [])
            self.doc.setdefault("samplers", [{"magFilter": 9728, "minFilter": 9728}])
            bv = self.view(texture_png)
            self.doc["images"].append({"bufferView": bv, "mimeType": "image/png"})
            self.doc["textures"].append({"source": len(self.doc["images"]) - 1, "sampler": 0})
            pbr["baseColorTexture"] = {"index": len(self.doc["textures"]) - 1}
            pbr["baseColorFactor"] = [1.0, 1.0, 1.0, lin[3]]
        if any(mat.get("emissive", [0, 0, 0])):
            out["emissiveFactor"] = [float(x) for x in srgb_to_linear(np.array(mat["emissive"]))]
        if mat["base_color"][3] < 0.999:
            out["alphaMode"] = "BLEND"
        if mat.get("double_sided"):
            out["doubleSided"] = True
        self.doc["materials"].append(out)
        return len(self.doc["materials"]) - 1

    def add_mesh(self, m: Mesh, texture: np.ndarray | None = None) -> int:
        s = _unroll(m)
        prims = []
        tex_png = None
        if texture is not None:
            import io
            buf = io.BytesIO()
            Image.fromarray(texture, "RGBA").save(buf, "PNG")
            tex_png = buf.getvalue()
        shared_tex_mat = None
        for mi in range(len(m.materials)):
            sel = np.nonzero(s["mat"] == mi)[0]
            if not len(sel):
                continue
            attrs = {"POSITION": self.accessor(s["pos"][sel], "VEC3", 34962, minmax=True),
                     "NORMAL": self.accessor(s["nrm"][sel], "VEC3", 34962)}
            if "uv" in s:
                uv = s["uv"][sel].copy()
                uv[:, 1] = 1.0 - uv[:, 1]  # glTF UV origin is top-left
                attrs["TEXCOORD_0"] = self.accessor(uv, "VEC2", 34962)
            if "joints" in s:
                attrs["JOINTS_0"] = self.accessor(s["joints"][sel], "VEC4", 34962)
                attrs["WEIGHTS_0"] = self.accessor(s["weights"][sel], "VEC4", 34962)
            idx = np.arange(len(sel), dtype=np.uint32 if len(sel) > 65535 else np.uint16)
            if tex_png is not None:
                if shared_tex_mat is None:
                    shared_tex_mat = self.material({**m.materials[0], "name": f"{m.name}_atlas",
                                                    "base_color": [1, 1, 1, 1]}, tex_png)
                mat_index = shared_tex_mat
            else:
                mat_index = self.material(m.materials[mi])
            prims.append({"attributes": attrs, "indices": self.accessor(idx, "SCALAR", 34963), "material": mat_index})
        self.doc["meshes"].append({"name": m.name, "primitives": prims})
        return len(self.doc["meshes"]) - 1

    def node(self, **kw: Any) -> int:
        self.doc["nodes"].append({k: v for k, v in kw.items() if v is not None})
        return len(self.doc["nodes"]) - 1

    def finish(self, path: Path) -> list[Path]:
        path = Path(path)
        doc = {k: v for k, v in self.doc.items() if v != [] or k in ("scenes", "nodes")}
        while len(self.bin) % 4:
            self.bin.append(0)
        if path.suffix.lower() == ".gltf":
            doc["buffers"] = [{"byteLength": len(self.bin), "uri": "data:application/octet-stream;base64," + base64.b64encode(bytes(self.bin)).decode()}]
            path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            return [path]
        doc["buffers"] = [{"byteLength": len(self.bin)}]
        js = json.dumps(doc, separators=(",", ":")).encode()
        js += b" " * ((4 - len(js) % 4) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        with path.open("wb") as fh:
            fh.write(struct.pack("<4sII", b"glTF", 2, total))
            fh.write(struct.pack("<I4s", len(js), b"JSON"))
            fh.write(js)
            fh.write(struct.pack("<I4s", len(self.bin), b"BIN\x00"))
            fh.write(bytes(self.bin))
        return [path]


def write_gltf(m: Mesh, path: Path, texture: np.ndarray | None = None) -> list[Path]:
    b = GLTFBuilder()
    mi = b.add_mesh(m, texture)
    b.doc["scenes"][0]["nodes"].append(b.node(name=m.name, mesh=mi))
    return b.finish(path)


_COMP = {5126: ("<f4", 4), 5123: ("<u2", 2), 5125: ("<u4", 4), 5121: ("<u1", 1), 5122: ("<i2", 2), 5120: ("<i1", 1)}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def load_gltf_doc(path: Path) -> tuple[dict[str, Any], bytes]:
    path = Path(path)
    raw = path.read_bytes()
    if raw[:4] == b"glTF":
        _, _, total = struct.unpack("<4sII", raw[:12])
        off, doc, binchunk = 12, None, b""
        while off < min(total, len(raw)):
            ln, typ = struct.unpack("<I4s", raw[off:off + 8])
            chunk = raw[off + 8: off + 8 + ln]
            if typ == b"JSON":
                doc = json.loads(chunk.decode("utf-8"))
            elif typ.startswith(b"BIN"):
                binchunk = chunk
            off += 8 + ln
        if doc is None:
            raise ValueError("GLB has no JSON chunk.")
        return doc, binchunk
    doc = json.loads(raw.decode("utf-8"))
    buf = b""
    if doc.get("buffers"):
        uri = doc["buffers"][0].get("uri", "")
        if uri.startswith("data:"):
            buf = base64.b64decode(uri.split(",", 1)[1])
        elif uri:
            ext = (path.parent / uri).resolve()
            if path.parent.resolve() not in ext.parents:
                raise PermissionError("glTF buffer points outside its folder.")
            buf = ext.read_bytes()
    return doc, buf


def read_accessor(doc: dict[str, Any], buf: bytes, idx: int) -> np.ndarray:
    acc = doc["accessors"][idx]
    dt, size = _COMP[acc["componentType"]]
    nc = _NCOMP[acc["type"]]
    count = acc["count"]
    if "bufferView" not in acc:
        return np.zeros((count, nc))
    bv = doc["bufferViews"][acc["bufferView"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride", 0) or size * nc
    if stride == size * nc:
        arr = np.frombuffer(buf, dtype=dt, count=count * nc, offset=start).reshape(count, nc)
    else:
        arr = np.stack([np.frombuffer(buf, dtype=dt, count=nc, offset=start + i * stride) for i in range(count)])
    return arr.astype(np.float64)


def _node_matrix(node: dict[str, Any]) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    T = np.eye(4)
    T[:3, 3] = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    R = np.eye(4)
    R[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                 [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                 [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    S = np.diag(list(node.get("scale", [1, 1, 1])) + [1])
    return T @ R @ S


def read_gltf(path: Path) -> Mesh:
    doc, buf = load_gltf_doc(path)
    parts: list[Mesh] = []
    mats_doc = doc.get("materials", [])

    def to_mat(i: int | None) -> dict[str, Any]:
        if i is None or i >= len(mats_doc):
            return make_material("default")
        md = mats_doc[i]
        pbr = md.get("pbrMetallicRoughness", {})
        lin = pbr.get("baseColorFactor", [0.8, 0.8, 0.8, 1])
        col = [float(x) for x in linear_to_srgb(np.array(lin[:3]))] + [float(lin[3])]
        return {"name": md.get("name", f"mat{i}"), "base_color": col, "metallic": float(pbr.get("metallicFactor", 1.0)),
                "roughness": float(pbr.get("roughnessFactor", 1.0)),
                "emissive": [float(x) for x in linear_to_srgb(np.array(md.get("emissiveFactor", [0, 0, 0])))],
                "pattern": "", "double_sided": bool(md.get("doubleSided", False))}

    def walk(ni: int, parent: np.ndarray) -> None:
        node = doc["nodes"][ni]
        M = parent @ _node_matrix(node)
        if "mesh" in node:
            for prim in doc["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                P = read_accessor(doc, buf, prim["attributes"]["POSITION"])
                P = (np.c_[P, np.ones(len(P))] @ M.T)[:, :3]
                if "indices" in prim:
                    I = read_accessor(doc, buf, prim["indices"]).astype(np.int64).reshape(-1, 3)
                else:
                    I = np.arange(len(P)).reshape(-1, 3)
                part = _mk(P, I, to_mat(prim.get("material")), doc["meshes"][node["mesh"]].get("name", "mesh"))
                if "TEXCOORD_0" in prim["attributes"]:
                    uv = read_accessor(doc, buf, prim["attributes"]["TEXCOORD_0"])
                    uv[:, 1] = 1 - uv[:, 1]
                    part.UV = uv[I]
                if np.linalg.det(M[:3, :3]) < 0:
                    part.F = part.F[:, ::-1].copy()
                parts.append(part)
        for c in node.get("children", []):
            walk(c, M)

    scene = doc.get("scenes", [{}])[doc.get("scene", 0)] if doc.get("scenes") else {"nodes": list(range(len(doc.get("nodes", []))))}
    for ni in scene.get("nodes", []):
        walk(ni, np.eye(4))
    if any(p.UV is not None for p in parts):
        for p in parts:
            if p.UV is None:
                p.UV = np.zeros((len(p.F), 3, 2))
    m = merge(parts, Path(path).stem)
    return m


def load(path: str | Path) -> Mesh:
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".obj":
        return read_obj(p)
    if ext == ".stl":
        return read_stl(p)
    if ext in (".glb", ".gltf"):
        return read_gltf(p)
    raise ValueError(f"Unsupported 3D format '{ext}'. Use OBJ, STL, glTF or GLB.")


def save(m: Mesh, path: Path, texture: np.ndarray | None = None) -> list[Path]:
    ext = Path(path).suffix.lower()
    if ext == ".obj":
        return write_obj(m, path, texture)
    if ext == ".stl":
        return write_stl(m, path)
    if ext in (".glb", ".gltf"):
        return write_gltf(m, path, texture)
    raise ValueError(f"Unsupported 3D format '{ext}'.")


# ---------------------------------------------------------------------------
# Texture baking: material colours + patterns into a UV atlas
# ---------------------------------------------------------------------------


def uv_coverage_mask(m: Mesh, width: int, height: int) -> np.ndarray:
    """Pixels of a width x height texture that some triangle's UVs cover."""
    mask = Image.new("L", (int(width), int(height)), 0)
    draw = ImageDraw.Draw(mask)
    for tri in m.UV:
        pts = [(float(u) * width, (1.0 - float(v)) * height) for u, v in tri]
        draw.polygon(pts, fill=255)
    return np.asarray(mask) > 0


def bake_atlas(m: Mesh, size: int = 256, seed: int = 0) -> np.ndarray:
    """Paint each face's material (with its pattern) into the mesh's UV layout."""
    if m.UV is None:
        raise ValueError("Mesh needs UVs before baking; use uv='unwrap'.")
    size = int(size)
    tex = np.zeros((size, size, 4), dtype=np.uint8)
    pats = {}
    for i, mat in enumerate(m.materials):
        pats[i] = pattern_texture(mat.get("pattern") or "flat", mat["base_color"], 64, seed + i) if mat.get("pattern") else None
    uv = m.UV
    for f in range(len(m.F)):
        mi = int(m.face_mat[f])
        px = uv[f, :, 0] * size
        py = (1 - uv[f, :, 1]) * size
        minx, maxx = max(int(px.min()) - 1, 0), min(int(px.max()) + 1, size - 1)
        miny, maxy = max(int(py.min()) - 1, 0), min(int(py.max()) + 1, size - 1)
        ys, xs = np.mgrid[miny:maxy + 1, minx:maxx + 1] + 0.5
        (x0, x1, x2), (y0, y1, y2) = px, py
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-12:
            continue
        w0 = ((x1 - xs) * (y2 - ys) - (x2 - xs) * (y1 - ys)) / area
        w1 = ((x2 - xs) * (y0 - ys) - (x0 - xs) * (y2 - ys)) / area
        w2 = 1 - w0 - w1
        pad = 1.5 / max(abs(area) ** 0.5, 1)  # dilate a little to avoid seams
        inside = (w0 >= -pad) & (w1 >= -pad) & (w2 >= -pad)
        sub = tex[miny:maxy + 1, minx:maxx + 1]
        if pats[mi] is not None:
            pt = pats[mi]
            ty = (ys.astype(int) % pt.shape[0])
            tx = (xs.astype(int) % pt.shape[1])
            sub[inside] = pt[ty[inside], tx[inside]]
        else:
            sub[inside] = (np.array(m.materials[mi]["base_color"]) * 255 + 0.5).astype(np.uint8)
    return tex


# ---------------------------------------------------------------------------
# Building from a spec (what Claude sends)
# ---------------------------------------------------------------------------


def apply_ops(m: Mesh, ops: list[dict[str, Any]], materials: dict[str, dict[str, Any]]) -> Mesh:
    for op in ops or []:
        kind = op.get("op")
        if kind == "extrude":
            m = extrude(m, op.get("select", "all"), float(op.get("distance", 0.1)), float(op.get("scale", 1.0)), op.get("direction"))
        elif kind == "inset":
            m = extrude(m, op.get("select", "all"), 0.0, float(op.get("scale", 0.8)))
        elif kind == "subdivide":
            m = subdivide(m, bool(op.get("smooth", True)), int(op.get("levels", 1)))
        elif kind == "mirror":
            m = mirror(m, op.get("axis", "x"), bool(op.get("weld", True)))
        elif kind == "decimate":
            m = decimate(m, int(op.get("target_faces", 0)), float(op.get("ratio", 0.5)))
        elif kind == "transform":
            m = transform(m, op.get("translate", (0, 0, 0)), op.get("rotate", (0, 0, 0)), op.get("scale", 1.0))
        elif kind == "taper":
            m = taper(m, op.get("axis", "y"), float(op.get("amount", 0.5)))
        elif kind == "twist":
            m = twist(m, op.get("axis", "y"), float(op.get("degrees", 45)))
        elif kind == "jitter":
            m = jitter(m, float(op.get("amount", 0.05)), int(op.get("seed", 0)))
        elif kind == "smooth":
            m = smooth_laplacian(m, int(op.get("iterations", 2)), float(op.get("factor", 0.5)))
        elif kind == "weld":
            m = weld(m, float(op.get("tolerance", 1e-6)))
        elif kind == "orient":
            m = orient_outward(m)
        elif kind == "flip":
            m = flip(m)
        elif kind == "material":
            name = op.get("material", "default")
            mat = materials.get(name) or make_material(name)
            m = set_material(m, mat, op.get("select", "all"))
        elif kind == "shade":
            m.smooth = op.get("mode", "flat") == "smooth"
        else:
            raise ValueError(f"Unknown mesh op '{kind}'. Ops: extrude, inset, subdivide, mirror, decimate, transform, taper, twist, jitter, smooth, weld, orient, flip, material, shade.")
    return m


def build_part(part: dict[str, Any], materials: dict[str, dict[str, Any]]) -> Mesh:
    shape = part.get("shape", "box")
    if shape not in PRIMITIVES:
        raise ValueError(f"Unknown shape '{shape}'. Shapes: {', '.join(PRIMITIVES)}.")
    mname = part.get("material", "default")
    mat = materials.get(mname) or make_material(mname)
    m = PRIMITIVES[shape](part, mat)
    m = apply_ops(m, part.get("ops", []), materials)
    m = transform(m, part.get("position", (0, 0, 0)), part.get("rotation", (0, 0, 0)), part.get("scale", 1.0))
    m.name = str(part.get("name") or shape)
    if part.get("shading") == "smooth":
        m.smooth = True
    return m


def build_spec(spec: dict[str, Any]) -> tuple[Mesh, list[Mesh], dict[str, dict[str, Any]]]:
    materials = {name: make_material(name, s) for name, s in (spec.get("materials") or {}).items()}
    parts = [build_part(p, materials) for p in spec.get("parts") or []]
    if spec.get("source"):
        from .studio import resolve_input
        parts.insert(0, load(resolve_input(spec["source"])))
    if not parts:
        raise ValueError("Give at least one part (or a source file).")
    m = merge(parts, spec.get("name", "model"))
    m = apply_ops(m, spec.get("ops", []), materials)
    if spec.get("shading") == "smooth":
        m.smooth = True
    return m, parts, materials


def finish(m: Mesh, name: str, formats: Iterable[str] = ("glb",), uv: str = "", texture_size: int = 0,
           target: str = "generic", seed: int = 0, extra_info: dict[str, Any] | None = None,
           builder: GLTFBuilder | None = None) -> dict[str, Any]:
    """UV, bake, validate, export, render a preview sheet. Shared by mesh, lowpoly and rig tools."""
    from .studio import make_sheet, out_path, save_sheet, write_manifest
    texture = None
    if uv == "box":
        m = uv_box(m)
    elif uv == "unwrap":
        m = uv_unwrap(m)
    if texture_size:
        if m.UV is None:
            m = uv_unwrap(m)
        texture = bake_atlas(m, int(texture_size), seed)
    report = validate(m, target)
    boundary = report.pop("boundary")
    split = report.pop("_split")
    files: list[str] = []
    for fmt in formats:
        fmt = fmt.lower().lstrip(".")
        if fmt not in ("glb", "gltf", "obj", "stl"):
            raise ValueError(f"Unknown export format '{fmt}'.")
        p = out_path("mesh", name, "." + fmt)
        if builder is not None and fmt in ("glb", "gltf"):
            files += [str(x) for x in builder.finish(p)]
        else:
            files += [str(x) for x in save(m, p, texture)]
    panels = []
    lohi = m.bounds()
    for label in ("three_quarter", "front", "right", "top"):
        yaw, pitch = VIEWS[label]
        img = render(m, 256, yaw, pitch, texture=texture, fit_bounds=lohi)
        if label == "three_quarter" and len(boundary):
            img = highlight_edges(img, split, boundary, yaw, pitch)
        panels.append((label.replace("_", " "), img))
    panels.append(("wireframe", render(m, 256, *VIEWS["three_quarter"], wire=True, fit_bounds=lohi)))
    panels.append(("back", render(m, 256, *VIEWS["back"], texture=texture, fit_bounds=lohi)))
    if texture is not None:
        panels.append(("texture atlas", Image.fromarray(texture, "RGBA")))
    st = report["stats"]
    lines = [
        f"{st['triangles']} triangles, {st['welded_vertices']} vertices, {st['materials']} materials, "
        f"size {st['size_m'][0]} x {st['size_m'][1]} x {st['size_m'][2]} m",
        f"watertight: {st['watertight']}  holes: {st['holes']}  non-manifold: {st['non_manifold_edges']}  "
        f"degenerate: {st['degenerate_faces']}  slivers: {st['sliver_faces']}",
    ]
    if st["uv"].get("has_uv"):
        lines.append(f"UV stretch mean {st['uv']['mean_stretch']} max {st['uv']['max_stretch']}  texel spread {st['uv']['texel_density_spread']}")
    lines.append(f"Verdict: {report['verdict']}   (pink = back faces showing, red = open edges)")
    sheet = make_sheet(f"3D: {name}", panels, lines, report["issues"], cols=4)
    sheet_path = save_sheet(sheet, "mesh", name)
    info = {"kind": "mesh", "name": name, "files": files, "preview": str(sheet_path), **report, **(extra_info or {})}
    if files:
        write_manifest(Path(files[0]), {k: v for k, v in info.items() if k != "files"} | {"files": files})
    return info


def review_file(path: str) -> dict[str, Any]:
    from .studio import make_sheet, save_sheet
    p = Path(path)
    m = load(p)
    report = validate(m)
    boundary = report.pop("boundary")
    split = report.pop("_split")
    lohi = m.bounds()
    panels = []
    for label in ("three_quarter", "front", "right", "top", "back"):
        yaw, pitch = VIEWS[label]
        img = render(m, 256, yaw, pitch, fit_bounds=lohi)
        if label == "three_quarter" and len(boundary):
            img = highlight_edges(img, split, boundary, yaw, pitch)
        panels.append((label.replace("_", " "), img))
    panels.append(("wireframe", render(m, 256, *VIEWS["three_quarter"], wire=True, fit_bounds=lohi)))
    st = report["stats"]
    lines = [f"{st['triangles']} triangles, size {st['size_m']} m, watertight {st['watertight']}, holes {st['holes']}",
             f"Verdict: {report['verdict']}   (pink = back faces showing, red = open edges)"]
    sheet = save_sheet(make_sheet(f"Review: {p.name}", panels, lines, report["issues"], cols=3), "mesh", "review_" + p.stem)
    return {"path": str(p), "preview": str(sheet), **report}


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def _build_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    m, _, _ = build_spec(args)
    name = safe_name(args.get("name", "model"))
    return finish(m, name, args.get("formats") or ["glb"], args.get("uv", ""), int(args.get("texture_size", 0)),
                  args.get("target", "generic"), int(args.get("seed", 0)))


def _edit_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(args["source"])
    materials = {n: make_material(n, s) for n, s in (args.get("materials") or {}).items()}
    m = apply_ops(load(src), args.get("ops", []), materials)
    name = safe_name(args.get("name") or src.stem + "_edit")
    return finish(m, name, args.get("formats") or [src.suffix.lstrip(".") if src.suffix.lower() != ".stl" else "stl"],
                  args.get("uv", ""), int(args.get("texture_size", 0)), args.get("target", "generic"))


def _convert_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(args["source"])
    m = load(src)
    if args.get("scale"):
        m = transform(m, scale=float(args["scale"]))
    return finish(m, safe_name(args.get("name") or src.stem), args.get("formats") or ["glb"], args.get("uv", ""),
                  int(args.get("texture_size", 0)), args.get("target", "generic"))


def _repair_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(args["source"])
    m = load(src)
    before = validate(m)
    before.pop("boundary")
    before.pop("_split")
    m = remove_unused(orient_outward(weld(m, float(args.get("tolerance", 1e-5)))))
    srt = np.sort(m.F, axis=1)
    _, uniq = np.unique(srt, axis=0, return_index=True)
    uniq = np.sort(uniq)
    m.F, m.face_mat = m.F[uniq], m.face_mat[uniq]
    if m.UV is not None:
        m.UV = m.UV[uniq]
    info = finish(m, safe_name(args.get("name") or src.stem + "_repaired"), args.get("formats") or ["glb"], target=args.get("target", "generic"))
    info["before"] = before["stats"]
    info["note"] = "Welded duplicate vertices, removed duplicate/degenerate faces, made winding consistent and outward. Holes are reported, not filled."
    return info


def _materials_cap(args: dict[str, Any]) -> dict[str, Any]:
    out = []
    for name in sorted(MATERIAL_LIBRARY):
        mat = make_material(name)
        out.append({"name": name, "base_color": to_hex(mat["base_color"]), "metallic": mat["metallic"],
                    "roughness": mat["roughness"], "pattern": mat["pattern"], "alpha": mat["base_color"][3]})
    return {"materials": out, "shapes": list(PRIMITIVES),
            "ops": ["extrude", "inset", "subdivide", "mirror", "decimate", "transform", "taper", "twist", "jitter",
                    "smooth", "weld", "orient", "flip", "material", "shade"],
            "selection": "'all' | [face ids] | {normal:[0,1,0], threshold:0.9} | {material:'wood'} | {above:{axis:'y',value:1}}"}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                return {"status": "ok", "summary": f"{summary} Verdict: {data.get('verdict', 'n/a')}.", "data": data}
            except (ValueError, FileNotFoundError, PermissionError, KeyError, IndexError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    part_doc = ("parts: [{shape: box|rounded_box|plane|wedge|cylinder|cone|sphere|icosphere|torus|capsule, size/radius/height/segments..., "
                "position:[x,y,z], rotation:[deg x,y,z], scale, material, ops:[...]}]")
    reg.register(CapabilitySpec(
        id="mesh.build", area="3d",
        summary="Build a 3D model from parts and edit ops (extrude, inset, subdivide, mirror, taper, twist, decimate...), "
                "give it PBR materials, UVs and an optional baked texture, validate it, export GLB/glTF/OBJ/STL, and render a preview sheet.",
        input_schema={"name": "str", "parts": "optional list[dict]: " + part_doc, "materials": "optional dict {name: preset name or {preset, base_color, metallic, roughness, emissive, alpha}}", "ops": "optional list[dict]",
                      "uv": "optional str (box|unwrap)", "texture_size": "optional int", "formats": "optional list[str]",
                      "target": "optional str (generic|roblox|minecraft|web)", "shading": "optional str (flat|smooth)",
                      "source": "optional str", "seed": "optional int"},
        permission="read", handler=wrap(_build_cap, "Model built."), tags=["3d", "mesh", "create", "lowpoly"],
    ))
    reg.register(CapabilitySpec(
        id="mesh.edit", area="3d",
        summary="Apply edit ops to an existing OBJ/STL/glTF/GLB and save the result to the studio (the original is untouched).",
        input_schema={"source": "str", "ops": "list[dict]", "name": "optional str", "materials": "optional dict",
                      "uv": "optional str", "texture_size": "optional int", "formats": "optional list[str]", "target": "optional str"},
        permission="read", handler=wrap(_edit_cap, "Model edited."), tags=["3d", "mesh", "edit"],
    ))
    reg.register(CapabilitySpec(
        id="mesh.convert", area="3d",
        summary="Convert between OBJ, STL, glTF and GLB (optionally rescale, add UVs or bake a texture).",
        input_schema={"source": "str", "formats": "list[str]", "scale": "optional float", "uv": "optional str", "name": "optional str"},
        permission="read", handler=wrap(_convert_cap, "Model converted."), tags=["3d", "convert", "export"],
    ))
    reg.register(CapabilitySpec(
        id="mesh.clean", area="3d",
        summary="Clean a mesh natively into the studio (no trimesh needed): weld, remove duplicate and degenerate faces, fix winding and inverted normals. Reports holes.",
        input_schema={"source": "str", "tolerance": "optional float", "name": "optional str", "formats": "optional list[str]"},
        permission="read", handler=wrap(_repair_cap, "Mesh repaired."), tags=["3d", "repair", "qa"],
    ))
    reg.register(CapabilitySpec(
        id="mesh.library", area="3d",
        summary="List 3D shapes, edit ops, face selections and the PBR material library.",
        input_schema={}, permission="read",
        handler=lambda a: {"status": "ok", "summary": "3D library listed.", "data": _materials_cap(a)}, tags=["3d", "materials"],
    ))
