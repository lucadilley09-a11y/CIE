"""Parametric low-poly game asset generators (Phase 3B, advanced).

Each generator returns a mesh.build spec, so the result is editable: Claude can
read the spec, change a part, and rebuild with mesh.build. Every model is made
of closed parts, uses metres, sits on the ground (y = 0) and faces +Z.
"""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(int(seed))


def tree(seed: int = 0, height: float = 4.0, style: str = "round", **_: Any) -> dict[str, Any]:
    r = _rng(seed)
    h = float(height)
    trunk_h = h * (0.45 if style == "round" else 0.3)
    parts: list[dict[str, Any]] = [{"name": "trunk", "shape": "cylinder", "radius": h * 0.06, "radius_top": h * 0.04,
                                    "height": trunk_h, "segments": 7, "position": [0, trunk_h / 2, 0], "material": "bark",
                                    "rotation": [0, float(r.uniform(0, 360)), 0]}]
    if style == "pine":
        tiers = 4
        for i in range(tiers):
            t = i / tiers
            rad = h * (0.34 - 0.07 * i) * float(r.uniform(0.92, 1.08))
            tier_h = h * 0.32
            parts.append({"name": f"tier{i}", "shape": "cone", "radius": rad, "height": tier_h, "segments": 8,
                          "position": [0, trunk_h * 0.7 + t * h * 0.62 + tier_h / 2, 0],
                          "rotation": [0, float(r.uniform(0, 45)), 0], "material": "needles"})
        mats = {"bark": {"preset": "dark_wood"}, "needles": {"preset": "pine"}}
    else:
        blobs = 3 + int(r.integers(0, 3))
        crown_y = trunk_h + h * 0.22
        for i in range(blobs):
            ang = 2 * math.pi * i / blobs + float(r.uniform(-0.4, 0.4))
            off = h * 0.14 if i else 0.0
            rad = h * float(r.uniform(0.2, 0.28)) if i else h * 0.3
            parts.append({"name": f"leaves{i}", "shape": "icosphere", "radius": rad, "subdivisions": 1,
                          "position": [off * math.cos(ang), crown_y + float(r.uniform(-0.1, 0.25)) * h * (1 if i else 0), off * math.sin(ang)],
                          "scale": [1, float(r.uniform(0.8, 0.95)), 1], "material": "leaves",
                          "ops": [{"op": "jitter", "amount": rad * 0.12, "seed": int(seed) * 7 + i}]})
        mats = {"bark": {"preset": "wood", "base_color": "#6b4a2f"}, "leaves": {"preset": "leaves"}}
    return {"name": f"tree_{style}", "parts": parts, "materials": mats}


def rock(seed: int = 0, size: float = 1.0, **_: Any) -> dict[str, Any]:
    r = _rng(seed)
    s = float(size)
    sc = [float(r.uniform(0.8, 1.25)), float(r.uniform(0.5, 0.8)), float(r.uniform(0.8, 1.2))]
    return {"name": "rock", "materials": {"rock": {"preset": "stone"}}, "parts": [
        {"shape": "icosphere", "radius": s / 2, "subdivisions": 1, "material": "rock", "scale": sc,
         "position": [0, s / 2 * sc[1] * 0.8, 0], "rotation": [0, float(r.uniform(0, 360)), 0],
         "ops": [{"op": "jitter", "amount": s * 0.09, "seed": int(seed)}]}]}


def barrel(seed: int = 0, height: float = 1.0, **_: Any) -> dict[str, Any]:
    h = float(height)
    rad = h * 0.36
    seg = 12
    parts = [
        {"name": "lower", "shape": "cylinder", "radius": rad * 0.9, "radius_top": rad, "height": h * 0.34, "segments": seg, "position": [0, h * 0.17, 0], "material": "staves"},
        {"name": "middle", "shape": "cylinder", "radius": rad, "height": h * 0.34, "segments": seg, "position": [0, h * 0.5, 0], "material": "staves"},
        {"name": "upper", "shape": "cylinder", "radius": rad, "radius_top": rad * 0.9, "height": h * 0.34, "segments": seg, "position": [0, h * 0.83, 0], "material": "staves"},
    ]
    for i, y in enumerate((0.16, 0.84)):
        parts.append({"name": f"band{i}", "shape": "cylinder", "radius": rad * (0.97 if i else 0.97) + 0.012, "height": h * 0.06, "segments": seg,
                      "position": [0, h * y, 0], "material": "band"})
    return {"name": "barrel", "parts": parts, "materials": {"staves": {"preset": "planks"}, "band": {"preset": "iron"}}}


def crate(seed: int = 0, size: float = 1.0, **_: Any) -> dict[str, Any]:
    s = float(size)
    parts = [{"name": "body", "shape": "box", "size": [s * 0.94] * 3, "position": [0, s / 2, 0], "material": "planks"}]
    e = s * 0.08
    for x in (-1, 1):
        for z in (-1, 1):
            parts.append({"name": "post", "shape": "box", "size": [e, s, e], "position": [x * (s / 2 - e / 2), s / 2, z * (s / 2 - e / 2)], "material": "frame"})
    for y in (e / 2, s - e / 2):
        for axis in ("x", "z"):
            for side in (-1, 1):
                size = [s, e, e] if axis == "x" else [e, e, s]
                pos = [0, y, side * (s / 2 - e / 2)] if axis == "x" else [side * (s / 2 - e / 2), y, 0]
                parts.append({"name": "rail", "shape": "box", "size": size, "position": pos, "material": "frame"})
    return {"name": "crate", "parts": parts, "materials": {"planks": {"preset": "planks"}, "frame": {"preset": "dark_wood"}}}


def house(seed: int = 0, width: float = 4.0, depth: float = 3.0, height: float = 2.6, **_: Any) -> dict[str, Any]:
    w, d, h = float(width), float(depth), float(height)
    roof_h = w * 0.35
    slope = math.hypot(w / 2, roof_h)
    ang = math.degrees(math.atan2(roof_h, w / 2))
    t = 0.12
    parts = [
        {"name": "walls", "shape": "box", "size": [w, h, d], "position": [0, h / 2, 0], "material": "walls"},
        {"name": "gable", "shape": "wedge", "size": [d, roof_h, w / 2], "position": [-w / 4, h + roof_h / 2, 0], "rotation": [0, 90, 0], "material": "walls"},
        {"name": "gable2", "shape": "wedge", "size": [d, roof_h, w / 2], "position": [w / 4, h + roof_h / 2, 0], "rotation": [0, -90, 0], "material": "walls"},
        {"name": "roof_l", "shape": "box", "size": [slope + 0.3, t, d + 0.4], "position": [-w / 4 - 0.05, h + roof_h / 2 + t / 2, 0], "rotation": [0, 0, ang], "material": "roof"},
        {"name": "roof_r", "shape": "box", "size": [slope + 0.3, t, d + 0.4], "position": [w / 4 + 0.05, h + roof_h / 2 + t / 2, 0], "rotation": [0, 0, -ang], "material": "roof"},
        {"name": "door", "shape": "box", "size": [0.9, 1.9, 0.08], "position": [0, 0.95, d / 2 + 0.03], "material": "door"},
        {"name": "chimney", "shape": "box", "size": [0.45, 1.2, 0.45], "position": [w * 0.25, h + roof_h * 0.7, -d * 0.2], "material": "brick"},
    ]
    for x in (-w * 0.3, w * 0.3):
        parts.append({"name": "window", "shape": "box", "size": [0.7, 0.7, 0.06], "position": [x, h * 0.6, d / 2 + 0.02], "material": "glass"})
        parts.append({"name": "sill", "shape": "box", "size": [0.85, 0.08, 0.14], "position": [x, h * 0.6 - 0.39, d / 2 + 0.05], "material": "trim"})
    return {"name": "house", "parts": parts, "materials": {"walls": {"preset": "planks", "base_color": "#c9b48a"}, "roof": {"preset": "brick", "base_color": "#7a3b2e"},
                                                         "door": {"preset": "dark_wood"}, "brick": {"preset": "brick"},
                                                         "glass": {"preset": "glass", "alpha": 0.6}, "trim": {"preset": "wood"}}}


def fence(seed: int = 0, length: float = 3.0, posts: int = 4, **_: Any) -> dict[str, Any]:
    L, n = float(length), max(2, int(posts))
    parts = []
    for i in range(n):
        x = -L / 2 + L * i / (n - 1)
        parts.append({"name": f"post{i}", "shape": "box", "size": [0.12, 1.1, 0.12], "position": [x, 0.55, 0], "material": "wood",
                      "ops": [{"op": "extrude", "select": {"normal": [0, 1, 0]}, "distance": 0.08, "scale": 0.0}]})
    for y in (0.35, 0.8):
        parts.append({"name": "rail", "shape": "box", "size": [L, 0.1, 0.05], "position": [0, y, 0.08], "material": "wood"})
    return {"name": "fence", "parts": parts, "materials": {"wood": {"preset": "wood"}}}


def sword(seed: int = 0, length: float = 1.0, **_: Any) -> dict[str, Any]:
    L = float(length)
    blade = L * 0.7
    return {"name": "sword", "materials": {"blade": {"preset": "steel"}, "guard": {"preset": "gold"}, "grip": {"preset": "leather"}}, "parts": [
        {"name": "blade", "shape": "box", "size": [L * 0.08, blade, L * 0.018], "position": [0, L * 0.25 + blade / 2, 0], "material": "blade",
         "ops": [{"op": "extrude", "select": {"normal": [0, 1, 0]}, "distance": L * 0.07, "scale": 0.0}]},
        {"name": "guard", "shape": "box", "size": [L * 0.26, L * 0.04, L * 0.06], "position": [0, L * 0.25, 0], "material": "guard"},
        {"name": "grip", "shape": "cylinder", "radius": L * 0.022, "height": L * 0.2, "segments": 8, "position": [0, L * 0.13, 0], "material": "grip"},
        {"name": "pommel", "shape": "icosphere", "radius": L * 0.04, "subdivisions": 1, "position": [0, L * 0.02, 0], "material": "guard"},
    ]}


def chest(seed: int = 0, width: float = 1.0, **_: Any) -> dict[str, Any]:
    w = float(width)
    d, h = w * 0.6, w * 0.45
    return {"name": "chest", "materials": {"wood": {"preset": "wood"}, "metal": {"preset": "gold"}}, "parts": [
        {"name": "base", "shape": "box", "size": [w, h, d], "position": [0, h / 2, 0], "material": "wood"},
        {"name": "lid", "shape": "cylinder", "radius": d / 2, "height": w, "segments": 10, "position": [0, h, 0], "rotation": [0, 0, 90], "material": "wood",
         "ops": []},
        {"name": "band_l", "shape": "box", "size": [w * 0.06, h * 1.02, d * 1.04], "position": [-w * 0.32, h / 2, 0], "material": "metal"},
        {"name": "band_r", "shape": "box", "size": [w * 0.06, h * 1.02, d * 1.04], "position": [w * 0.32, h / 2, 0], "material": "metal"},
        {"name": "lock", "shape": "box", "size": [w * 0.12, w * 0.14, w * 0.05], "position": [0, h * 0.95, d / 2 + w * 0.02], "material": "metal"},
    ]}


def crystal(seed: int = 0, size: float = 1.0, count: int = 5, **_: Any) -> dict[str, Any]:
    r = _rng(seed)
    s = float(size)
    parts = []
    for i in range(max(1, int(count))):
        hgt = s * float(r.uniform(0.5, 1.0)) * (1.3 if i == 0 else 1)
        tilt = 0 if i == 0 else float(r.uniform(15, 40))
        yaw = float(r.uniform(0, 360))
        parts.append({"name": f"shard{i}", "shape": "cylinder", "radius": s * 0.12, "height": hgt, "segments": 6,
                      "ops": [{"op": "extrude", "select": {"normal": [0, 1, 0]}, "distance": s * 0.15, "scale": 0.0}],
                      "position": [math.sin(math.radians(yaw)) * s * 0.12 * (i > 0), hgt / 2 * math.cos(math.radians(tilt)), math.cos(math.radians(yaw)) * s * 0.12 * (i > 0)],
                      "rotation": [tilt, yaw, 0], "material": "crystal"})
    parts.append({"name": "base", "shape": "icosphere", "radius": s * 0.25, "subdivisions": 0, "scale": [1, 0.4, 1], "position": [0, 0.02, 0], "material": "rock"})
    return {"name": "crystal", "parts": parts, "materials": {"crystal": {"base_color": "#7fd6ff", "roughness": 0.15, "emissive": "#103040"}, "rock": {"preset": "dark_stone"}}}


def potion(seed: int = 0, size: float = 0.3, color: str = "#d6336c", **_: Any) -> dict[str, Any]:
    s = float(size)
    return {"name": "potion", "materials": {"glass": {"preset": "glass", "alpha": 0.5}, "liquid": {"base_color": color, "roughness": 0.2, "emissive": "#200008"},
                                            "cork": {"preset": "wood", "base_color": "#b08a5a"}}, "parts": [
        {"name": "liquid", "shape": "sphere", "radius": s * 0.36, "segments": 12, "rings": 6, "position": [0, s * 0.38, 0], "material": "liquid"},
        {"name": "bottle", "shape": "sphere", "radius": s * 0.4, "segments": 12, "rings": 6, "position": [0, s * 0.4, 0], "material": "glass"},
        {"name": "neck", "shape": "cylinder", "radius": s * 0.12, "height": s * 0.3, "segments": 10, "position": [0, s * 0.85, 0], "material": "glass"},
        {"name": "cork", "shape": "cylinder", "radius": s * 0.1, "radius_top": s * 0.12, "height": s * 0.12, "segments": 8, "position": [0, s * 1.03, 0], "material": "cork"},
    ]}


def lamp_post(seed: int = 0, height: float = 3.0, **_: Any) -> dict[str, Any]:
    h = float(height)
    return {"name": "lamp_post", "materials": {"iron": {"preset": "rubber", "base_color": "#3a3d42", "roughness": 0.5}, "light": {"base_color": "#ffe9a8", "emissive": "#ffd36b"}}, "parts": [
        {"name": "base", "shape": "cylinder", "radius": 0.18, "radius_top": 0.12, "height": 0.3, "segments": 8, "position": [0, 0.15, 0], "material": "iron"},
        {"name": "pole", "shape": "cylinder", "radius": 0.06, "height": h, "segments": 8, "position": [0, h / 2, 0], "material": "iron"},
        {"name": "lamp", "shape": "cylinder", "radius": 0.16, "radius_top": 0.22, "height": 0.35, "segments": 6, "position": [0, h + 0.15, 0], "material": "light"},
        {"name": "cap", "shape": "cone", "radius": 0.28, "height": 0.25, "segments": 6, "position": [0, h + 0.45, 0], "material": "iron"},
    ]}


def mushroom(seed: int = 0, size: float = 0.5, **_: Any) -> dict[str, Any]:
    s = float(size)
    return {"name": "mushroom", "materials": {"stem": {"preset": "bone"}, "cap": {"preset": "plastic", "base_color": "#c8332f", "roughness": 0.6}}, "parts": [
        {"name": "stem", "shape": "cylinder", "radius": s * 0.12, "radius_top": s * 0.09, "height": s * 0.5, "segments": 8, "position": [0, s * 0.25, 0], "material": "stem"},
        {"name": "cap", "shape": "sphere", "radius": s * 0.4, "segments": 10, "rings": 6, "scale": [1, 0.55, 1], "position": [0, s * 0.52, 0], "material": "cap"},
    ]}


GENERATORS: dict[str, Callable[..., dict[str, Any]]] = {
    "tree": tree, "rock": rock, "barrel": barrel, "crate": crate, "house": house, "fence": fence, "sword": sword,
    "chest": chest, "crystal": crystal, "potion": potion, "lamp_post": lamp_post, "mushroom": mushroom,
}


def generate(kind: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in GENERATORS:
        raise ValueError(f"Unknown generator '{kind}'. Choose from: {', '.join(GENERATORS)}.")
    params = dict(params or {})
    spec = GENERATORS[kind](**params)
    if params.get("name"):
        spec["name"] = params["name"]
    return spec


def _generate_cap(args: dict[str, Any]) -> dict[str, Any]:
    from . import mesh3d
    from .studio import safe_name
    spec = generate(args["kind"], args.get("params") or {})
    m, _, _ = mesh3d.build_spec(spec)
    name = safe_name(args.get("name") or spec["name"] + (f"_{args['params']['seed']}" if (args.get("params") or {}).get("seed") else ""))
    info = mesh3d.finish(m, name, args.get("formats") or ["glb"], args.get("uv", ""), int(args.get("texture_size", 0)),
                         args.get("target", "generic"), int((args.get("params") or {}).get("seed", 0)))
    info["spec"] = spec
    info["note"] = "Edit 'spec' and pass it to mesh.build to change any part."
    return info


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = _generate_cap(args)
            return {"status": "ok", "summary": f"Generated {args['kind']}. Verdict: {data['verdict']}.", "data": data}
        except (ValueError, TypeError, KeyError) as exc:
            return {"status": "error", "summary": str(exc)}

    reg.register(CapabilitySpec(
        id="mesh.generate", area="3d",
        summary="Generate a low-poly game asset (tree, rock, barrel, crate, house, fence, sword, chest, crystal, potion, lamp_post, mushroom) "
                "with seed/size params. Returns the editable build spec, files and a preview sheet.",
        input_schema={"kind": "str", "params": "optional dict (seed, size/height/width, style...)", "name": "optional str",
                      "formats": "optional list[str]", "uv": "optional str", "texture_size": "optional int", "target": "optional str"},
        permission="read", handler=handler, tags=["3d", "lowpoly", "procedural", "generate", "props"],
    ))
