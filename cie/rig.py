"""Rigs and animation (Phase 3C, 3D first).

  * Roblox-R15-style humanoid characters built from parts, each part bound to a bone.
  * Automatic skinning for any imported mesh (fits a humanoid skeleton to it).
  * Keyframe clips: per-bone rotation (and root translation) keys with easing,
    quaternion interpolation, baked to a frame rate.
  * Procedural presets: idle, walk, run, jump, wave, attack, with legs driven by
    two-bone IK so planted feet do not slide.
  * Clip tools: retime, blend two clips, mirror left/right.
  * Object animation for props (spin, bob, hinge) with no skeleton.
  * Quality metrics: loop seam, popping, foot sliding (and the ground speed that
    makes an in-place cycle slide-free), ground penetration.
  * Export: GLB with skin, joints and every clip; preview sheet + GIF.

Characters face +Z (glTF convention), so their left side is +X.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw

from . import mesh3d as M

# ---------------------------------------------------------------------------
# Quaternions (x, y, z, w)
# ---------------------------------------------------------------------------


def q_from_euler(deg: Any) -> np.ndarray:
    R = M.rotation_matrix(deg)
    return q_from_matrix(R)


def q_from_matrix(R: np.ndarray) -> np.ndarray:
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s, 0.25 * s]
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = [0.25 * s, (R[0, 1] + R[1, 0]) / s, (R[0, 2] + R[2, 0]) / s, (R[2, 1] - R[1, 2]) / s]
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = [(R[0, 1] + R[1, 0]) / s, 0.25 * s, (R[1, 2] + R[2, 1]) / s, (R[0, 2] - R[2, 0]) / s]
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = [(R[0, 2] + R[2, 0]) / s, (R[1, 2] + R[2, 1]) / s, 0.25 * s, (R[1, 0] - R[0, 1]) / s]
    q = np.array(q)
    return q / np.linalg.norm(q)


def q_to_matrix(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def q_slerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    d = float(a @ b)
    if d < 0:
        b, d = -b, -d
    if d > 0.9995:
        r = a + t * (b - a)
        return r / np.linalg.norm(r)
    th = math.acos(min(1.0, d))
    return (math.sin((1 - t) * th) * a + math.sin(t * th) * b) / math.sin(th)


def q_angle(a: np.ndarray, b: np.ndarray) -> float:
    return math.degrees(2 * math.acos(min(1.0, abs(float(a @ b)))))


EASINGS: dict[str, Callable[[float], float]] = {
    "linear": lambda t: t,
    "in": lambda t: t * t,
    "out": lambda t: 1 - (1 - t) * (1 - t),
    "inout": lambda t: t * t * (3 - 2 * t),
    "step": lambda t: 0.0,
    "back": lambda t: t * t * (2.70158 * t - 1.70158),
    "elastic": lambda t: 1 - math.cos(t * math.pi * 4.5) * math.exp(-6 * t) if t > 0 else 0.0,
}

# ---------------------------------------------------------------------------
# Skeleton
# ---------------------------------------------------------------------------


@dataclass
class Bone:
    name: str
    parent: int
    head: np.ndarray  # rest position, world space


@dataclass
class Rig:
    name: str
    bones: list[Bone]
    mesh: M.Mesh
    params: dict[str, Any] = field(default_factory=dict)

    def index(self, name: str) -> int:
        for i, b in enumerate(self.bones):
            if b.name == name:
                return i
        raise KeyError(f"No bone '{name}'. Bones: {', '.join(b.name for b in self.bones)}")

    def local_offsets(self) -> np.ndarray:
        return np.array([b.head - (self.bones[b.parent].head if b.parent >= 0 else np.zeros(3)) for b in self.bones])

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params,
                "bones": [{"name": b.name, "parent": b.parent, "head": [float(x) for x in b.head]} for b in self.bones]}


def world_matrices(rig: Rig, rots: np.ndarray, root_offset: np.ndarray | None = None) -> np.ndarray:
    """rots: (bones, 4) local rotations. Returns (bones, 4, 4) world matrices."""
    off = rig.local_offsets()
    W = np.zeros((len(rig.bones), 4, 4))
    for i, b in enumerate(rig.bones):
        L = np.eye(4)
        L[:3, :3] = q_to_matrix(rots[i])
        L[:3, 3] = off[i] + (root_offset if (root_offset is not None and b.parent < 0) else 0)
        W[i] = (W[b.parent] @ L) if b.parent >= 0 else L
    return W


def pose_mesh(rig: Rig, rots: np.ndarray, root_offset: np.ndarray | None = None) -> M.Mesh:
    W = world_matrices(rig, rots, root_offset)
    heads = np.array([b.head for b in rig.bones])
    skin = W.copy()
    skin[:, :3, 3] = W[:, :3, 3] - np.einsum("bij,bj->bi", W[:, :3, :3], heads)
    m = rig.mesh.copy()
    V = np.c_[m.V, np.ones(len(m.V))]
    out = np.zeros((len(V), 3))
    for k in range(4):
        j = m.joints[:, k]
        w = m.weights[:, k]
        out += w[:, None] * np.einsum("nij,nj->ni", skin[j], V)[:, :3]
    m.V = out
    return m


# ---------------------------------------------------------------------------
# Humanoid character (R15-style)
# ---------------------------------------------------------------------------

R15 = ["HumanoidRootPart", "LowerTorso", "UpperTorso", "Head",
       "LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand",
       "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "RightUpperLeg", "RightLowerLeg", "RightFoot"]


def humanoid_dims(height: float = 1.8, build: str = "normal") -> dict[str, float]:
    h = float(height)
    k = {"slim": 0.85, "normal": 1.0, "broad": 1.18, "chibi": 1.0}.get(build, 1.0)
    chibi = build == "chibi"
    head = h * (0.24 if chibi else 0.135)
    leg = h * (0.33 if chibi else 0.47)
    torso = h - head - leg
    return {"h": h, "head": head, "leg": leg, "upper_leg": leg * 0.5, "lower_leg": leg * 0.42, "foot_h": leg * 0.08,
            "torso": torso, "lower_torso": torso * 0.3, "upper_torso": torso * 0.7,
            "shoulder_w": h * 0.24 * k * (1.1 if chibi else 1), "hip_w": h * 0.1 * k, "limb": h * 0.075 * k,
            "arm": torso * 1.02, "depth": h * 0.11 * k}


def humanoid_skeleton(d: dict[str, float]) -> list[Bone]:
    hip_y = d["leg"]
    lt = hip_y
    ut = hip_y + d["lower_torso"]
    neck = hip_y + d["torso"]
    sh_y = neck - d["limb"] * 0.6
    sx = d["shoulder_w"] / 2 + d["limb"] / 2
    ua, la = d["arm"] * 0.42, d["arm"] * 0.4
    hx = d["hip_w"] / 2 + d["limb"] * 0.1
    ul, ll = d["upper_leg"], d["lower_leg"]
    B = [("HumanoidRootPart", -1, [0, hip_y, 0]), ("LowerTorso", 0, [0, lt, 0]), ("UpperTorso", 1, [0, ut, 0]),
         ("Head", 2, [0, neck, 0])]
    for side, sgn in (("Left", 1), ("Right", -1)):
        p = len(B)
        B += [(f"{side}UpperArm", 2, [sgn * sx, sh_y, 0]), (f"{side}LowerArm", p, [sgn * sx, sh_y - ua, 0]),
              (f"{side}Hand", p + 1, [sgn * sx, sh_y - ua - la, 0])]
    for side, sgn in (("Left", 1), ("Right", -1)):
        p = len(B)
        B += [(f"{side}UpperLeg", 1, [sgn * hx, hip_y, 0]), (f"{side}LowerLeg", p, [sgn * hx, hip_y - ul, 0]),
              (f"{side}Foot", p + 1, [sgn * hx, hip_y - ul - ll, 0])]
    return [Bone(n, p, np.array(h, dtype=np.float64)) for n, p, h in B]


def build_humanoid(name: str = "character", height: float = 1.8, build: str = "normal", style: str = "blocky",
                   colors: dict[str, str] | None = None, hair: str = "short", accessory: str = "") -> Rig:
    d = humanoid_dims(height, build)
    bones = humanoid_skeleton(d)
    col = {"skin": "#e0b18f", "shirt": "#3b6fd1", "pants": "#2f3b52", "shoes": "#2a2420", "hair": "#4a2f1d",
           "eyes": "#1c1c22", "accent": "#d9b44a"}
    col.update(colors or {})
    mats = {k: M.make_material(k, {"base_color": v, "roughness": 0.65 if k != "eyes" else 0.3,
                                   "pattern": "noise" if k in ("shirt", "pants") else ""}) for k, v in col.items()}
    rounded = style != "blocky"
    parts: list[tuple[M.Mesh, str]] = []

    def block(size: list[float], centre: list[float], mat: str, bone: str, taper: float = 0.0) -> None:
        if rounded:
            m = M.rounded_box(size, min(size) * 0.18, 1, mats[mat])
        else:
            m = M.box(size, mats[mat])
        if taper:
            m = M.taper(m, "y", taper)
        parts.append((M.transform(m, centre), bone))

    B = {b.name: b.head for b in bones}
    lim, dep = d["limb"], d["depth"]
    # torso
    block([d["shoulder_w"], d["upper_torso"], dep], [0, B["UpperTorso"][1] + d["upper_torso"] / 2, 0], "shirt", "UpperTorso")
    block([d["shoulder_w"] * 0.92, d["lower_torso"], dep * 0.95], [0, B["LowerTorso"][1] + d["lower_torso"] / 2, 0], "pants", "LowerTorso")
    # head + face
    hy = B["Head"][1] + d["head"] / 2 + d["head"] * 0.05
    hw = d["head"] * (1.05 if build == "chibi" else 0.9)
    block([hw, d["head"], hw], [0, hy, 0], "skin", "Head")
    eye = d["head"] * 0.12
    for sgn in (1, -1):
        parts.append((M.transform(M.box([eye, eye * 1.4, eye * 0.3], mats["eyes"]), [sgn * hw * 0.2, hy + d["head"] * 0.05, hw / 2 + eye * 0.1]), "Head"))
    if hair != "none":
        hh = d["head"] * (0.28 if hair == "short" else 0.45)
        parts.append((M.transform(M.box([hw * 1.08, hh, hw * 1.08], mats["hair"]), [0, hy + d["head"] / 2 - hh / 2 + d["head"] * 0.06, -hw * 0.02]), "Head"))
        if hair == "long":
            parts.append((M.transform(M.box([hw * 1.08, d["head"] * 0.9, hw * 0.3], mats["hair"]), [0, hy - d["head"] * 0.1, -hw * 0.45]), "Head"))
    if accessory == "hat":
        parts.append((M.transform(M.cylinder(hw * 0.75, d["head"] * 0.15, 10, None, mats["accent"]), [0, hy + d["head"] * 0.6, 0]), "Head"))
        parts.append((M.transform(M.cylinder(hw * 0.45, d["head"] * 0.4, 10, None, mats["accent"]), [0, hy + d["head"] * 0.8, 0]), "Head"))
    # arms
    for side in ("Left", "Right"):
        ua, la, hd = B[f"{side}UpperArm"], B[f"{side}LowerArm"], B[f"{side}Hand"]
        block([lim, ua[1] - la[1] + lim * 0.3, lim], [ua[0], (ua[1] + la[1]) / 2 + lim * 0.15, 0], "shirt", f"{side}UpperArm")
        block([lim * 0.92, la[1] - hd[1], lim * 0.92], [la[0], (la[1] + hd[1]) / 2, 0], "skin", f"{side}LowerArm")
        block([lim * 0.95, lim * 1.1, lim * 0.7], [hd[0], hd[1] - lim * 0.55, 0], "skin", f"{side}Hand")
    # legs
    for side in ("Left", "Right"):
        ul, ll, ft = B[f"{side}UpperLeg"], B[f"{side}LowerLeg"], B[f"{side}Foot"]
        block([lim * 1.15, ul[1] - ll[1], lim * 1.15], [ul[0], (ul[1] + ll[1]) / 2, 0], "pants", f"{side}UpperLeg")
        block([lim * 1.05, ll[1] - ft[1], lim * 1.05], [ll[0], (ll[1] + ft[1]) / 2, 0], "pants", f"{side}LowerLeg")
        block([lim * 1.1, d["foot_h"], lim * 1.7], [ft[0], ft[1] - d["foot_h"] / 2, lim * 0.3], "shoes", f"{side}Foot")
    names = [b.name for b in bones]
    meshes = []
    for m, bone in parts:
        j = names.index(bone)
        m.joints = np.tile(np.array([j, 0, 0, 0], dtype=np.int64), (len(m.V), 1))
        m.weights = np.tile(np.array([1.0, 0, 0, 0]), (len(m.V), 1))
        meshes.append(m)
    mesh = M.merge(meshes, name)
    # The feet bottoms sit at y = 0 when the root is at hip height.
    params = {"kind": "humanoid", "height": height, "build": build, "style": style, "colors": col, "hair": hair,
              "accessory": accessory, "dims": d, "foot_bottom": float(B["LeftFoot"][1] - d["foot_h"])}
    return Rig(name, bones, mesh, params)


def auto_rig(mesh: M.Mesh, name: str = "rigged") -> Rig:
    """Fit a humanoid skeleton to any mesh standing on y=0 facing +Z, then skin it automatically
    (each vertex weighted to its nearest bones by distance, limbs kept to their side)."""
    lo, hi = mesh.bounds()
    h = float(hi[1] - lo[1])
    if h <= 0:
        raise ValueError("Mesh has no height.")
    d = humanoid_dims(h)
    width = float(hi[0] - lo[0])
    d["shoulder_w"] = max(width * 0.42, d["shoulder_w"] * 0.6)
    bones = humanoid_skeleton(d)
    shift = np.array([(lo[0] + hi[0]) / 2, lo[1] - (bones[12].head[1] - d["lower_leg"] - d["foot_h"]) * 0, (lo[2] + hi[2]) / 2])
    for b in bones:
        b.head = b.head + np.array([shift[0], lo[1], shift[2]])
    heads = np.array([b.head for b in bones])
    tails = heads.copy()
    for i, b in enumerate(bones):
        kids = [j for j, c in enumerate(bones) if c.parent == i]
        tails[i] = heads[kids[0]] if kids else heads[i] + (heads[i] - heads[b.parent]) * 0.4 if b.parent >= 0 else heads[i]
    V = mesh.V
    dist = np.zeros((len(V), len(bones)))
    for i in range(len(bones)):
        a, b = heads[i], tails[i]
        ab = b - a
        t = np.clip(((V - a) @ ab) / max(ab @ ab, 1e-12), 0, 1)
        dist[:, i] = np.linalg.norm(V - (a + t[:, None] * ab), axis=1)
    dist[:, 0] = np.inf  # root carries nothing
    for i, b in enumerate(bones):
        side = 1 if b.name.startswith("Left") else -1 if b.name.startswith("Right") else 0
        if side:
            wrong = (V[:, 0] - shift[0]) * side < -1e-4
            dist[wrong, i] = np.inf
    w = 1.0 / np.maximum(dist, 1e-4) ** 4
    top = np.argsort(-w, axis=1)[:, :4]
    tw = np.take_along_axis(w, top, axis=1)
    tw /= tw.sum(axis=1, keepdims=True)
    out = mesh.copy()
    out.joints, out.weights = top.astype(np.int64), tw
    return Rig(name, bones, out, {"kind": "humanoid", "height": h, "dims": d, "auto_weights": True, "foot_bottom": float(lo[1])})


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------


@dataclass
class Clip:
    name: str
    length: float  # seconds
    fps: int
    loop: bool
    rots: np.ndarray       # (frames, bones, 4)
    root: np.ndarray       # (frames, 3) root offset from rest
    in_place: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def frames(self) -> int:
        return len(self.rots)

    @property
    def times(self) -> np.ndarray:
        return np.arange(self.frames) / self.fps


def _blank(rig: Rig, length: float, fps: int) -> tuple[np.ndarray, np.ndarray, int]:
    n = int(round(length * fps)) + 1
    rots = np.zeros((n, len(rig.bones), 4))
    rots[..., 3] = 1
    return rots, np.zeros((n, 3)), n


def from_keys(rig: Rig, name: str, length: float, keys: dict[str, list[dict[str, Any]]], fps: int = 30,
              loop: bool = True, root_keys: list[dict[str, Any]] | None = None, interp: str = "smooth") -> Clip:
    """Keyframes: {bone: [{t: sec, rot: [x,y,z] deg, ease}]}.
    interp='smooth' (default): a Catmull-Rom curve through all keys, so speed never jumps.
    interp='eased': each segment uses the easing named on the key it moves into
    (linear|in|out|inout|step|back|elastic) - use it for deliberate snaps and holds."""
    rots, root, n = _blank(rig, length, fps)
    times = np.arange(n) / fps
    for bone, ks in (keys or {}).items():
        bi = rig.index(bone)
        ks = sorted(ks, key=lambda k: float(k["t"]))
        ts = [float(k["t"]) for k in ks]
        if interp == "smooth":
            eul = [k.get("rot", (0, 0, 0)) for k in ks]
            for f, t in enumerate(times):
                rots[f, bi] = q_from_euler(smooth_curve(ts, eul, t, loop, length))
            continue
        qs = [q_from_euler(k.get("rot", (0, 0, 0))) for k in ks]
        for f, t in enumerate(times):
            rots[f, bi] = _sample(ts, qs, [k.get("ease", "inout") for k in ks], t, loop, length, slerp=True)
    if root_keys:
        rk = sorted(root_keys, key=lambda k: float(k["t"]))
        ts = [float(k["t"]) for k in rk]
        ps = [np.array(k.get("pos", (0, 0, 0)), dtype=np.float64) for k in rk]
        for f, t in enumerate(times):
            root[f] = smooth_curve(ts, ps, t, loop, length) if interp == "smooth" else _sample(ts, ps, [k.get("ease", "inout") for k in rk], t, loop, length, slerp=False)
    return Clip(name, float(length), int(fps), bool(loop), rots, root)


def smooth_curve(ts: list[float], vals: list[Any], t: float, loop: bool = False, length: float = 0.0) -> np.ndarray:
    """Catmull-Rom through keyframes: passes every key, speed never jumps (C1). Ends ease to rest
    unless the clip loops, in which case the curve wraps around."""
    ts = [float(x) for x in ts]
    P = [np.atleast_1d(np.asarray(v, dtype=np.float64)) for v in vals]
    n = len(ts)
    if n == 1:
        return P[0]
    if loop and length > ts[-1]:
        ts = [ts[-1] - length] + ts + [ts[0] + length]
        P = [P[-1]] + P + [P[0]]
        if t < ts[1]:
            t = t if t >= ts[0] else t + length
    t = min(max(t, ts[0]), ts[-1])
    i = max(0, min(int(np.searchsorted(ts, t, side="right")) - 1, len(ts) - 2))
    t0, t1 = ts[i], ts[i + 1]
    dt = t1 - t0 or 1e-9

    def tangent(k: int) -> np.ndarray:
        if k <= 0 or k >= len(ts) - 1:
            return np.zeros_like(P[k])
        return (P[k + 1] - P[k - 1]) / (ts[k + 1] - ts[k - 1])

    u = (t - t0) / dt
    h00, h10, h01, h11 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u, -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
    return h00 * P[i] + h10 * dt * tangent(i) + h01 * P[i + 1] + h11 * dt * tangent(i + 1)


def _sample(ts: list[float], vals: list[np.ndarray], eases: list[str], t: float, loop: bool, length: float, slerp: bool) -> np.ndarray:
    if len(ts) == 1:
        return vals[0]
    if t <= ts[0]:
        if loop and ts[0] > 0:  # wrap from last key
            span = ts[0] + (length - ts[-1])
            u = (t + length - ts[-1]) / span if span > 0 else 0
            return _mix(vals[-1], vals[0], EASINGS.get(eases[0], EASINGS["inout"])(u), slerp)
        return vals[0]
    if t >= ts[-1]:
        if loop and ts[-1] < length:
            span = ts[0] + (length - ts[-1])
            u = (t - ts[-1]) / span if span > 0 else 0
            return _mix(vals[-1], vals[0], EASINGS.get(eases[0], EASINGS["inout"])(u), slerp)
        return vals[-1]
    i = int(np.searchsorted(ts, t, side="right")) - 1
    u = (t - ts[i]) / (ts[i + 1] - ts[i])
    return _mix(vals[i], vals[i + 1], EASINGS.get(eases[i + 1], EASINGS["inout"])(u), slerp)


def _mix(a: np.ndarray, b: np.ndarray, u: float, slerp: bool) -> np.ndarray:
    return q_slerp(a, b, u) if slerp else a + (b - a) * u


def _qx(deg: float) -> np.ndarray:
    r = math.radians(deg) / 2
    return np.array([math.sin(r), 0, 0, math.cos(r)])


def _qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz])


def _leg_ik(hip: np.ndarray, target: np.ndarray, l1: float, l2: float) -> tuple[float, float]:
    """Sagittal two-bone IK. Returns (thigh, knee) X rotations in degrees; knee bends forward."""
    dz, dy = target[2] - hip[2], target[1] - hip[1]
    d = min(math.hypot(dz, dy), (l1 + l2) * 0.9999)
    d = max(d, abs(l1 - l2) + 1e-6)
    phi = math.atan2(-dz, -dy)
    a = math.acos(max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d))))
    k = math.acos(max(-1.0, min(1.0, (l1 * l1 + l2 * l2 - d * d) / (2 * l1 * l2))))
    return math.degrees(phi - a), math.degrees(math.pi - k)


def _humanoid_frame(rig: Rig, f_rots: np.ndarray, root: np.ndarray, feet: dict[str, np.ndarray],
                    extra: dict[str, list[float]]) -> None:
    """Fill one frame: extra euler rotations on upper-body bones, then two-bone IK for each leg so the
    ankle lands exactly on its target. IK is solved in the pelvis frame and the foot is kept flat."""
    for bone, e in extra.items():
        f_rots[rig.index(bone)] = q_from_euler(e)
    lt = rig.index("LowerTorso")
    W = world_matrices(rig, f_rots, root)
    Wp = W[lt]
    Rp = Wp[:3, :3]
    inv = np.linalg.inv(Wp)
    for side in ("Left", "Right"):
        ui, li, fi = rig.index(f"{side}UpperLeg"), rig.index(f"{side}LowerLeg"), rig.index(f"{side}Foot")
        l1 = float(np.linalg.norm(rig.bones[li].head - rig.bones[ui].head))
        l2 = float(np.linalg.norm(rig.bones[fi].head - rig.bones[li].head))
        hip_local = rig.bones[ui].head - rig.bones[lt].head
        tgt_local = (inv @ np.r_[feet[side], 1.0])[:3]
        D = tgt_local - hip_local
        # roll the leg plane toward the target's sideways offset, then solve the 2D chain in that plane
        side_r = math.hypot(D[0], D[1])
        psi = math.atan2(D[0], -D[1])
        thigh, knee = _leg_ik(np.zeros(3), np.array([0.0, -side_r, D[2]]), l1, l2)
        f_rots[ui] = q_from_matrix(M.rotation_matrix([0, 0, math.degrees(psi)]) @ q_to_matrix(_qx(thigh)))
        f_rots[li] = _qx(knee)
        # keep the sole parallel to the ground: foot world rotation = identity
        R_leg = Rp @ q_to_matrix(f_rots[ui]) @ q_to_matrix(f_rots[li])
        f_rots[fi] = q_from_matrix(R_leg.T)


def _ankle_rest(rig: Rig, side: str) -> np.ndarray:
    return rig.bones[rig.index(f"{side}Foot")].head.copy()


def preset_locomotion(rig: Rig, mode: str = "walk", speed: float = 1.0, fps: int = 30, style: float = 1.0,
                      run_amount: float | None = None) -> Clip:
    """In-place walk/jog/run cycle. Planted feet move backward at exactly the ground speed.
    run_amount 0 = walk, 1 = run; values between give a jog with correct footwork."""
    w = float(run_amount) if run_amount is not None else (1.0 if mode == "run" else 0.0)
    w = min(1.0, max(0.0, w))

    def mix(walk_v: float, run_v: float) -> float:
        return walk_v + (run_v - walk_v) * w
    d = rig.params["dims"]
    leg = d["leg"]
    cycle = mix(1.0, 0.62) / max(0.25, float(speed))
    stride = leg * mix(1.35, 2.1) * (0.85 + 0.15 * float(speed))  # distance per full cycle
    v = stride / cycle
    contact = mix(0.62, 0.38)
    lift = leg * mix(0.12, 0.22)
    rots, root, n = _blank(rig, cycle, fps)
    l1 = rig.bones[rig.index("LeftUpperLeg")].head[1] - rig.bones[rig.index("LeftLowerLeg")].head[1]
    l2 = rig.bones[rig.index("LeftLowerLeg")].head[1] - rig.bones[rig.index("LeftFoot")].head[1]
    reach = (l1 + l2) * 0.96  # never fully straight: a locked knee makes IK speeds spike
    hip_rest = rig.bones[rig.index("LeftUpperLeg")].head[1]
    zc = v * contact * cycle / 2
    swing_t = (1 - contact) * cycle

    def foot_at(side: str, t: float) -> np.ndarray:
        p = (t + (0.0 if side == "Left" else 0.5)) % 1.0
        a = _ankle_rest(rig, side)
        if p < contact:
            return np.array([a[0], a[1], zc - 2 * zc * (p / contact)])
        u = (p - contact) / (1 - contact)
        # Hermite curve: leaves and lands with the planted foot's backward speed, so no hitch.
        m = -v * swing_t
        h00, h10, h01, h11 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u, -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
        z = h00 * (-zc) + h10 * m + h01 * zc + h11 * m
        # quick heel pop on lift-off, soft touch-down (zero vertical speed at contact)
        y = a[1] + lift * (math.sin(math.pi * u) if u < 0.5 else math.sin(math.pi * u) ** 2)
        return np.array([a[0], y, z])

    ts = [f / (n - 1) if n > 1 else 0.0 for f in range(n)]
    floor = min(min(foot_at(s_, t)[1] + math.sqrt(max(reach ** 2 - foot_at(s_, t)[2] ** 2, 0.01)) for s_ in ("Left", "Right")) for t in ts)
    base_y = min(hip_rest, floor) - leg * mix(0.01, 0.03)
    for f, t in enumerate(ts):
        feet = {s_: foot_at(s_, t) for s_ in ("Left", "Right")}
        bob = -(math.cos(4 * math.pi * t) * 0.5 + 0.5) * leg * mix(0.018, 0.035) * style
        root[f] = [0, base_y + bob - hip_rest, 0]
        swing = math.sin(2 * math.pi * t)
        arm = mix(22, 40) * style
        extra = {
            "LowerTorso": [mix(2, 8) * style, 6 * swing * style, 0],
            "UpperTorso": [mix(1, 6), -10 * swing * style, 0],
            "Head": [-mix(2, 8), 5 * swing * style, 0],
            "LeftUpperArm": [arm * swing, 0, 4], "RightUpperArm": [-arm * swing, 0, -4],
            "LeftLowerArm": [-mix(18, 55) - 6 * (1 + swing), 0, 0],
            "RightLowerArm": [-mix(18, 55) - 6 * (1 - swing), 0, 0],
        }
        _humanoid_frame(rig, rots[f], root[f], feet, extra)
    clip = Clip(mode if run_amount is None else f"jog{int(w * 100)}", cycle, fps, True, rots, root)
    clip.notes.append(f"In-place cycle. Move the character at {v:.2f} m/s (Roblox WalkSpeed {v / 0.28:.1f}) so planted feet don't slide.")
    clip.ground_speed = v  # type: ignore[attr-defined]
    return clip


def _planted(rig: Rig) -> dict[str, np.ndarray]:
    return {s: _ankle_rest(rig, s) for s in ("Left", "Right")}


def preset_idle(rig: Rig, fps: int = 30, length: float = 3.0, style: float = 1.0) -> Clip:
    rots, root, n = _blank(rig, length, fps)
    leg = rig.params["dims"]["leg"]
    for f in range(n):
        t = f / (n - 1)
        b = math.sin(2 * math.pi * t)
        root[f] = [0, -leg * (0.012 + 0.008 * (b * 0.5 + 0.5)) * style, 0]
        extra = {"UpperTorso": [1.5 * b * style, 0, 0], "Head": [-1.5 * b * style, 3 * math.sin(2 * math.pi * t + 1) * style, 0],
                 "LeftUpperArm": [2 * b, 0, 5 + 1.5 * b], "RightUpperArm": [2 * b, 0, -5 - 1.5 * b],
                 "LeftLowerArm": [-8 - 2 * b, 0, 0], "RightLowerArm": [-8 - 2 * b, 0, 0]}
        _humanoid_frame(rig, rots[f], root[f], _planted(rig), extra)
    return Clip("idle", length, fps, True, rots, root)


def _hermite(p0: float, v0: float, p1: float, v1: float, u: float, dur: float) -> float:
    h00, h10, h01, h11 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u, -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
    return h00 * p0 + h10 * v0 * dur + h01 * p1 + h11 * v1 * dur


def preset_jump(rig: Rig, fps: int = 30, height: float = 0.6, style: float = 1.0) -> Clip:
    """Anticipation crouch, push-off, airborne arc at real gravity (root motion), landing absorb, recover.
    Every phase hands its speed to the next, so nothing pops."""
    leg = rig.params["dims"]["leg"]
    g = 9.81
    up = math.sqrt(2 * g * float(height))
    air = 2 * up / g
    A, A2 = leg * 0.2, leg * 0.16
    tc, tp, tl, tr = 0.25, 0.12, 0.16, 0.35
    length = tc + tp + air + tl + tr
    rots, root, n = _blank(rig, length, fps)
    io = EASINGS["inout"]
    T1, T2, T3 = tc + tp, tc + tp + air, tc + tp + air + tl
    arm_keys = ([0, tc * 0.6, tc, T1, T1 + air * 0.3, T2, T3, length], [0, -35, -45, 70, 135, 45, 30, 0])
    lean_keys = ([0, tc, T1, T1 + air * 0.5, T2, T3, length], [0, 14, 4, -4, 2, 12, 0])
    for f in range(n):
        t = f / fps
        feet = _planted(rig)
        arms = float(smooth_curve(arm_keys[0], arm_keys[1], t)[0])
        lean = float(smooth_curve(lean_keys[0], lean_keys[1], t)[0])
        if t < tc:                       # anticipation
            y = -A * io(t / tc)
        elif t < T1:                     # push off: accelerate to take-off speed
            y = _hermite(-A, 0.0, 0.0, up, (t - tc) / tp, tp)
        elif t < T2:                     # airborne
            ta = t - T1
            y = up * ta - 0.5 * g * ta * ta
            tuck = leg * 0.18 * math.sin(math.pi * ta / air) ** 2
            for side in feet:
                feet[side] = feet[side] + np.array([0, y + tuck, 0.05 * tuck / leg])
        elif t < T3:                     # landing absorb
            y = _hermite(0.0, -up, -A2, 0.0, (t - T2) / tl, tl)
        else:                            # recover
            y = -A2 * (1 - io(min(1.0, (t - T3) / tr)))
        root[f] = [0, y, 0]
        extra = {"LowerTorso": [lean * style, 0, 0], "UpperTorso": [lean * 0.5 * style, 0, 0], "Head": [-lean * 0.6, 0, 0],
                 "LeftUpperArm": [-arms * style, 0, 8], "RightUpperArm": [-arms * style, 0, -8],
                 "LeftLowerArm": [-20, 0, 0], "RightLowerArm": [-20, 0, 0]}
        _humanoid_frame(rig, rots[f], root[f], feet, extra)
    clip = Clip("jump", length, fps, False, rots, root, in_place=False)
    clip.notes.append(f"Root motion: {height:.2f} m jump, {air:.2f} s airborne at real gravity.")
    return clip


def preset_wave(rig: Rig, fps: int = 30, length: float = 2.0, style: float = 1.0) -> Clip:
    keys = {"RightUpperArm": [{"t": 0, "rot": [0, 0, -5]}, {"t": 0.35, "rot": [0, 0, -150], "ease": "out"},
                              {"t": length - 0.35, "rot": [0, 0, -150]}, {"t": length, "rot": [0, 0, -5], "ease": "inout"}],
            "RightLowerArm": [{"t": 0, "rot": [0, 0, 0]}] + [
                {"t": 0.35 + i * 0.25, "rot": [0, 0, 30 if i % 2 else -10], "ease": "inout"} for i in range(int((length - 0.7) / 0.25) + 1)
            ] + [{"t": length, "rot": [0, 0, 0]}],
            "Head": [{"t": 0, "rot": [0, 0, 0]}, {"t": 0.4, "rot": [0, -8, 4]}, {"t": length - 0.3, "rot": [0, -8, 4]}, {"t": length, "rot": [0, 0, 0]}]}
    clip = from_keys(rig, "wave", length, keys, fps, loop=False)
    for f in range(clip.frames):
        keyed = clip.rots[f].copy()
        _humanoid_frame(rig, clip.rots[f], clip.root[f], _planted(rig), {})
        for b in keys:
            clip.rots[f, rig.index(b)] = keyed[rig.index(b)]
    return clip


def preset_attack(rig: Rig, fps: int = 30, style: float = 1.0) -> Clip:
    """Overhead swing: wind-up (anticipation), fast strike, hold, recover."""
    length = 0.9
    keys = {
        "RightUpperArm": [{"t": 0, "rot": [0, 0, -5]}, {"t": 0.28, "rot": [-150, 0, -20], "ease": "out"},
                          {"t": 0.4, "rot": [25, 0, -10], "ease": "in"}, {"t": 0.52, "rot": [45, 0, -10], "ease": "out"},
                          {"t": length, "rot": [0, 0, -5], "ease": "inout"}],
        "RightLowerArm": [{"t": 0, "rot": [-10, 0, 0]}, {"t": 0.28, "rot": [-60, 0, 0]}, {"t": 0.4, "rot": [-5, 0, 0], "ease": "in"},
                          {"t": length, "rot": [-10, 0, 0]}],
        "UpperTorso": [{"t": 0, "rot": [0, 0, 0]}, {"t": 0.28, "rot": [-6, 25, 0], "ease": "out"}, {"t": 0.4, "rot": [12, -20, 0], "ease": "in"},
                       {"t": length, "rot": [0, 0, 0]}],
        "LeftUpperArm": [{"t": 0, "rot": [0, 0, 5]}, {"t": 0.28, "rot": [-30, 0, 20]}, {"t": 0.4, "rot": [20, 0, 10]}, {"t": length, "rot": [0, 0, 5]}],
        "Head": [{"t": 0, "rot": [0, 0, 0]}, {"t": 0.28, "rot": [5, -15, 0]}, {"t": 0.4, "rot": [-8, 12, 0]}, {"t": length, "rot": [0, 0, 0]}],
    }
    base = from_keys(rig, "attack", length, keys, fps, loop=False)
    leg = rig.params["dims"]["leg"]
    for f in range(base.frames):
        t = f / fps
        dip = leg * 0.08 * math.sin(math.pi * min(1.0, max(0.0, (t - 0.2) / 0.5)))
        base.root[f] = [0, -dip, 0]
        saved = base.rots[f].copy()
        _humanoid_frame(rig, base.rots[f], base.root[f], _planted(rig), {})
        for b in keys:
            base.rots[f, rig.index(b)] = saved[rig.index(b)]
    return base


PRESETS = {
    "idle": lambda rig, p: preset_idle(rig, int(p.get("fps", 30)), float(p.get("length", 3.0)), float(p.get("style", 1.0))),
    "walk": lambda rig, p: preset_locomotion(rig, "walk", float(p.get("speed", 1.0)), int(p.get("fps", 30)), float(p.get("style", 1.0))),
    "run": lambda rig, p: preset_locomotion(rig, "run", float(p.get("speed", 1.0)), int(p.get("fps", 30)), float(p.get("style", 1.0))),
    "jump": lambda rig, p: preset_jump(rig, int(p.get("fps", 30)), float(p.get("height", 0.6)), float(p.get("style", 1.0))),
    "wave": lambda rig, p: preset_wave(rig, int(p.get("fps", 30)), float(p.get("length", 2.0))),
    "attack": lambda rig, p: preset_attack(rig, int(p.get("fps", 30))),
}


def retime(clip: Clip, factor: float) -> Clip:
    """Play factor x faster (2 = twice as fast) by resampling."""
    factor = float(factor)
    if factor <= 0:
        raise ValueError("Retime factor must be positive.")
    new_len = clip.length / factor
    n = int(round(new_len * clip.fps)) + 1
    src_t = np.linspace(0, clip.frames - 1, n)
    rots = np.zeros((n, clip.rots.shape[1], 4))
    root = np.zeros((n, 3))
    for i, s in enumerate(src_t):
        a = int(math.floor(s))
        b = min(a + 1, clip.frames - 1)
        u = s - a
        for j in range(clip.rots.shape[1]):
            rots[i, j] = q_slerp(clip.rots[a, j], clip.rots[b, j], u)
        root[i] = clip.root[a] * (1 - u) + clip.root[b] * u
    out = Clip(f"{clip.name}", new_len, clip.fps, clip.loop, rots, root, clip.in_place, list(clip.notes))
    if hasattr(clip, "ground_speed"):
        out.ground_speed = clip.ground_speed * factor  # type: ignore[attr-defined]
        out.notes = [f"In-place cycle. Move the character at {out.ground_speed:.2f} m/s so planted feet don't slide."]  # type: ignore[attr-defined]
    return out


def blend(a: Clip, b: Clip, weight: float, name: str = "") -> Clip:
    """Blend two looping clips by normalised time (e.g. walk/run at 0.5 = jog)."""
    w = min(1.0, max(0.0, float(weight)))
    length = a.length * (1 - w) + b.length * w
    n = int(round(length * a.fps)) + 1
    rots = np.zeros((n, a.rots.shape[1], 4))
    root = np.zeros((n, 3))
    for i in range(n):
        u = i / (n - 1) if n > 1 else 0
        fa, fb = u * (a.frames - 1), u * (b.frames - 1)
        ia, ib = int(round(fa)), int(round(fb))
        for j in range(rots.shape[1]):
            rots[i, j] = q_slerp(a.rots[ia, j], b.rots[ib, j], w)
        root[i] = a.root[ia] * (1 - w) + b.root[ib] * w
    out = Clip(name or f"{a.name}_{b.name}_{int(w * 100)}", length, a.fps, a.loop and b.loop, rots, root)
    if hasattr(a, "ground_speed") and hasattr(b, "ground_speed"):
        out.ground_speed = a.ground_speed * (1 - w) + b.ground_speed * w  # type: ignore[attr-defined]
    return out


def mirror_clip(rig: Rig, clip: Clip) -> Clip:
    """Swap left/right bones and mirror rotations across the X axis (a left-hand wave becomes right-hand)."""
    rots = clip.rots.copy()
    names = [b.name for b in rig.bones]
    for j, nme in enumerate(names):
        other = nme.replace("Left", "#").replace("Right", "Left").replace("#", "Right")
        k = names.index(other) if other in names else j
        q = clip.rots[:, k].copy()
        q[:, 1] *= -1
        q[:, 2] *= -1
        rots[:, j] = q
    root = clip.root.copy()
    root[:, 0] *= -1
    return Clip(clip.name + "_mirrored", clip.length, clip.fps, clip.loop, rots, root, clip.in_place, list(clip.notes))


# ---------------------------------------------------------------------------
# Quality metrics
# ---------------------------------------------------------------------------


def foot_track(rig: Rig, clip: Clip) -> dict[str, np.ndarray]:
    out = {}
    for side in ("Left", "Right"):
        try:
            fi = rig.index(f"{side}Foot")
        except KeyError:
            return {}
        pts = []
        for f in range(clip.frames):
            W = world_matrices(rig, clip.rots[f], clip.root[f])
            pts.append(W[fi, :3, 3])
        out[side] = np.array(pts)
    return out


def analyse(rig: Rig, clip: Clip) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    fps = clip.fps
    m: dict[str, Any] = {"frames": clip.frames, "fps": fps, "length_s": round(clip.length, 3), "loop": clip.loop}
    # loop seam
    if clip.loop and clip.frames > 2:
        seam = max(q_angle(clip.rots[0, j], clip.rots[-1, j]) for j in range(clip.rots.shape[1]))
        seam_root = float(np.linalg.norm(clip.root[0] - clip.root[-1]))
        nb = clip.rots.shape[1]
        speed = np.array([[q_angle(clip.rots[f, j], clip.rots[f + 1, j]) for j in range(nb)] for f in range(clip.frames - 1)])
        inner = np.abs(np.diff(speed, axis=0)).max() if len(speed) > 2 else 0.0
        seam_step = float(np.max(np.abs(speed[0] - speed[-1])))
        m["loop_seam_deg"] = round(seam, 3)
        m["loop_velocity_jump_deg_per_frame"] = round(seam_step, 3)
        m["max_inner_velocity_change_deg_per_frame"] = round(float(inner), 3)
        if seam > 0.5 or seam_root > 0.002:
            issues.append({"severity": "error", "message": f"Loop doesn't seal: last frame differs from first by {seam:.1f} deg / {seam_root * 100:.1f} cm."})
        elif seam_step > max(2.0, 1.5 * float(inner) + 0.5):
            issues.append({"severity": "warn", "message": "Loop seals in pose but motion speed jumps at the seam (visible hitch)."})
    # popping
    if clip.frames > 3:
        ang = np.array([[q_angle(clip.rots[f, j], clip.rots[f + 1, j]) for j in range(clip.rots.shape[1])]
                        for f in range(clip.frames - 1)]) * fps  # deg/s
        acc = np.abs(np.diff(ang, axis=0)) * fps  # deg/s^2
        m["max_angular_speed_deg_s"] = round(float(ang.max()), 1)
        m["max_angular_accel_deg_s2"] = round(float(acc.max()), 1)
        step = np.abs(np.diff(ang, axis=0))
        pops = int(np.sum(step > np.maximum(600.0, 6 * np.percentile(step, 95) + 1e-9)))
        m["pops"] = pops
        if pops:
            issues.append({"severity": "warn", "message": f"{pops} sudden speed changes (pops); ease into the key or add an in-between."})
    feet = foot_track(rig, clip)
    if feet:
        foot_h = rig.params.get("dims", {}).get("foot_h", 0.07)
        ground = rig.params.get("foot_bottom", 0.0)
        ys = np.concatenate([feet[s][:, 1] for s in feet]) - foot_h
        pen = float(ground - ys.min())
        m["ground_penetration_cm"] = round(max(0.0, pen) * 100, 2)
        if pen > 0.01:
            issues.append({"severity": "warn", "message": f"Feet go {pen * 100:.1f} cm below the ground."})
        slides, speeds = [], []
        for s, P in feet.items():
            low = P[:, 1] <= P[:, 1].min() + 0.003
            if low.sum() >= 3:
                vel = np.diff(P[:, [0, 2]], axis=0) * fps
                contact = low[:-1] & low[1:]
                if contact.sum() >= 2:
                    cv = vel[contact]
                    med = np.median(cv, axis=0)
                    speeds.append(float(-med[1]))
                    slides.append(float(np.mean(np.linalg.norm(cv - med, axis=1))) * contact.sum() / fps)
        if slides:
            m["foot_slide_cm"] = round(float(max(slides)) * 100, 2)
            if clip.in_place and speeds:
                m["ground_speed_m_s"] = round(float(np.mean(speeds)), 3)
            if max(slides) > 0.02:
                issues.append({"severity": "warn", "message": f"Planted feet slide {max(slides) * 100:.1f} cm per contact."})
    for note in clip.notes:
        m.setdefault("notes", []).append(note)
    from .studio import verdict
    return {"metrics": m, "issues": issues, "verdict": verdict(issues)}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_glb(rig: Rig, clips: list[Clip], path: Path) -> list[Path]:
    b = M.GLTFBuilder()
    off = rig.local_offsets()
    first = len(b.doc["nodes"])
    for i, bone in enumerate(rig.bones):
        b.node(name=bone.name, translation=[float(x) for x in off[i]])
    for i, bone in enumerate(rig.bones):
        kids = [first + j for j, c in enumerate(rig.bones) if c.parent == i]
        if kids:
            b.doc["nodes"][first + i]["children"] = kids
    ibm = np.zeros((len(rig.bones), 4, 4), dtype=np.float32)
    for i, bone in enumerate(rig.bones):
        mtx = np.eye(4)
        mtx[:3, 3] = -bone.head
        ibm[i] = mtx.T  # column-major
    ibm_acc = b.accessor(ibm.reshape(len(rig.bones), 16), "MAT4")
    b.doc["skins"] = [{"name": f"{rig.name}_skin", "joints": [first + i for i in range(len(rig.bones))],
                       "inverseBindMatrices": ibm_acc, "skeleton": first}]
    mesh_idx = b.add_mesh(rig.mesh)
    mesh_node = b.node(name=rig.name, mesh=mesh_idx, skin=0)
    b.doc["scenes"][0]["nodes"] = [first, mesh_node]
    anims = []
    for clip in clips:
        times = b.accessor(clip.times.astype(np.float32).reshape(-1, 1), "SCALAR", minmax=True)
        samplers, channels = [], []
        for j in range(len(rig.bones)):
            if np.allclose(clip.rots[:, j], clip.rots[0, j], atol=1e-6) and np.allclose(clip.rots[0, j], [0, 0, 0, 1], atol=1e-6):
                continue
            out = b.accessor(clip.rots[:, j].astype(np.float32), "VEC4")
            samplers.append({"input": times, "output": out, "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": first + j, "path": "rotation"}})
        tr = (off[0][None, :] + clip.root).astype(np.float32)
        out = b.accessor(tr, "VEC3")
        samplers.append({"input": times, "output": out, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": first, "path": "translation"}})
        anims.append({"name": clip.name, "samplers": samplers, "channels": channels})
    if anims:
        b.doc["animations"] = anims
    return b.finish(path)


def export_object_glb(m: M.Mesh, keys: list[dict[str, Any]], length: float, loop: bool, path: Path, fps: int = 30,
                      name: str = "anim") -> tuple[list[Path], np.ndarray]:
    """Animate a whole object: keys [{t, pos, rot (deg), scale, ease}]. Returns files and sampled TRS."""
    n = int(round(length * fps)) + 1
    times = np.arange(n) / fps
    ks = sorted(keys, key=lambda k: float(k["t"]))
    ts = [float(k["t"]) for k in ks]
    eases = [k.get("ease", "inout") for k in ks]
    P = [np.array(k.get("pos", (0, 0, 0)), dtype=np.float64) for k in ks]
    Q = [q_from_euler(k.get("rot", (0, 0, 0))) for k in ks]
    S = [np.array([k.get("scale", 1.0)] * 3 if np.isscalar(k.get("scale", 1.0)) else k["scale"], dtype=np.float64) for k in ks]
    pos = np.array([_sample(ts, P, eases, t, loop, length, False) for t in times])
    rot = np.array([_sample(ts, Q, eases, t, loop, length, True) for t in times])
    for i in range(1, len(rot)):  # keep quaternions in one hemisphere so LINEAR playback never flips
        if rot[i] @ rot[i - 1] < 0:
            rot[i] = -rot[i]
    scl = np.array([_sample(ts, S, eases, t, loop, length, False) for t in times])
    b = M.GLTFBuilder()
    mi = b.add_mesh(m)
    node = b.node(name=m.name, mesh=mi)
    b.doc["scenes"][0]["nodes"] = [node]
    tacc = b.accessor(times.astype(np.float32).reshape(-1, 1), "SCALAR", minmax=True)
    samplers = [{"input": tacc, "output": b.accessor(pos.astype(np.float32), "VEC3"), "interpolation": "LINEAR"},
                {"input": tacc, "output": b.accessor(rot.astype(np.float32), "VEC4"), "interpolation": "LINEAR"},
                {"input": tacc, "output": b.accessor(scl.astype(np.float32), "VEC3"), "interpolation": "LINEAR"}]
    channels = [{"sampler": i, "target": {"node": node, "path": p}} for i, p in enumerate(("translation", "rotation", "scale"))]
    b.doc["animations"] = [{"name": name, "samplers": samplers, "channels": channels}]
    return b.finish(path), np.concatenate([pos, rot, scl], axis=1)


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


def _frames_for_preview(rig: Rig, clip: Clip, count: int = 8) -> list[int]:
    return sorted({int(round(i * (clip.frames - 1) / max(1, count - 1))) for i in range(count)})


def preview(rig: Rig, clips: list[Clip], name: str, analyses: list[dict[str, Any]]) -> tuple[Path, list[Path]]:
    from .studio import make_sheet, out_path, save_sheet
    rest = np.zeros((len(rig.bones), 4))
    rest[:, 3] = 1
    rest_mesh = pose_mesh(rig, rest)
    los, his = [rest_mesh.bounds()[0]], [rest_mesh.bounds()[1]]
    for clip in clips:
        for f in _frames_for_preview(rig, clip, 6):
            b = pose_mesh(rig, clip.rots[f], clip.root[f]).bounds()
            los.append(b[0])
            his.append(b[1])
    lo, hi = np.min(los, axis=0), np.max(his, axis=0)
    span = float((hi - lo).max())
    centre = (lo + hi) / 2
    fit = (centre - span * 0.36, centre + span * 0.36)
    panels = [("rest pose", M.render(rest_mesh, 200, 30, 12, fit_bounds=fit))]
    gifs: list[Path] = []
    for clip, an in zip(clips, analyses):
        idx = _frames_for_preview(rig, clip, 6)
        for f in idx:
            pm = pose_mesh(rig, clip.rots[f], clip.root[f])
            panels.append((f"{clip.name} f{f}", M.render(pm, 200, 90 if clip.name in ("walk", "run") else 30, 8, fit_bounds=fit)))
        panels.append((f"{clip.name} feet", _foot_plot(rig, clip)))
        frames = []
        for f in range(0, clip.frames, max(1, clip.fps // 15)):
            pm = pose_mesh(rig, clip.rots[f], clip.root[f])
            im = M.render(pm, 160, 30, 10, fit_bounds=fit, bg=(40, 43, 50, 255))
            frames.append(im.convert("P", palette=Image.Palette.ADAPTIVE))
        gif = out_path("rig", f"{name}_{clip.name}", ".gif")
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=int(1000 / 15), loop=0)
        gifs.append(gif)
    lines = []
    for clip, an in zip(clips, analyses):
        mt = an["metrics"]
        bits = [f"{clip.name}: {mt['frames']} frames @ {mt['fps']} fps, {mt['length_s']} s"]
        for k in ("loop_seam_deg", "pops", "foot_slide_cm", "ground_speed_m_s", "ground_penetration_cm"):
            if k in mt:
                bits.append(f"{k} {mt[k]}")
        lines.append(", ".join(bits) + f"  -> {an['verdict']}")
    issues = [dict(i, message=f"{c.name}: {i['message']}") for c, an in zip(clips, analyses) for i in an["issues"]]
    sheet = make_sheet(f"Rig: {name} ({len(rig.bones)} bones)", panels, lines, issues, panel_size=200, cols=7)
    return save_sheet(sheet, "rig", name), gifs


def _foot_plot(rig: Rig, clip: Clip, size: int = 200) -> Image.Image:
    img = Image.new("RGBA", (size, size), (30, 32, 38, 255))
    d = ImageDraw.Draw(img)
    feet = foot_track(rig, clip)
    if not feet:
        return img
    allz = np.concatenate([feet[s][:, 2] for s in feet])
    ally = np.concatenate([feet[s][:, 1] for s in feet])
    zr = (allz.min(), allz.max() + 1e-6)
    yr = (ally.min(), ally.max() + 1e-6)
    for s, col in (("Left", (110, 200, 255, 255)), ("Right", (255, 170, 90, 255))):
        P = feet[s]
        n = len(P)
        pts_y = [(10 + (size - 20) * i / max(1, n - 1), size / 2 - 10 - (size / 2 - 20) * (P[i, 1] - yr[0]) / (yr[1] - yr[0])) for i in range(n)]
        pts_z = [(10 + (size - 20) * i / max(1, n - 1), size - 10 - (size / 2 - 20) * (P[i, 2] - zr[0]) / (zr[1] - zr[0])) for i in range(n)]
        d.line(pts_y, fill=col, width=2)
        d.line(pts_z, fill=col, width=1)
    d.text((6, 4), "foot height", fill=(200, 200, 200, 255))
    d.text((6, size / 2 + 2), "foot fwd/back", fill=(200, 200, 200, 255))
    return img


# ---------------------------------------------------------------------------
# Persistence + capabilities
# ---------------------------------------------------------------------------


def save_rig(rig: Rig) -> Path:
    from .studio import out_path
    p = out_path("rig", rig.name, ".rig.json")
    data = rig.to_json()
    data["mesh"] = {"V": rig.mesh.V.round(6).tolist(), "F": rig.mesh.F.tolist(), "face_mat": rig.mesh.face_mat.tolist(),
                    "materials": rig.mesh.materials, "joints": rig.mesh.joints.tolist(), "weights": rig.mesh.weights.round(5).tolist()}
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def load_rig(path: str) -> Rig:
    from .studio import resolve_input
    p = resolve_input(path)
    if not p.name.endswith(".rig.json"):
        cand = p.with_name(p.stem.split(".")[0] + ".rig.json")
        if cand.exists():
            p = cand
        else:
            raise ValueError("Pass the .rig.json CIE saved next to the character (rig.character / rig.auto_rig make one).")
    data = json.loads(p.read_text(encoding="utf-8"))
    md = data["mesh"]
    mesh = M.Mesh(np.array(md["V"]), np.array(md["F"], dtype=np.int64), np.array(md["face_mat"], dtype=np.int64), md["materials"],
                  None, data["name"], False, np.array(md["joints"], dtype=np.int64), np.array(md["weights"]))
    bones = [Bone(b["name"], int(b["parent"]), np.array(b["head"], dtype=np.float64)) for b in data["bones"]]
    return Rig(data["name"], bones, mesh, data.get("params", {}))


def make_clips(rig: Rig, specs: list[Any]) -> list[Clip]:
    clips: list[Clip] = []
    for spec in specs or []:
        if isinstance(spec, str):
            spec = {"preset": spec}
        if "preset" in spec:
            if spec["preset"] not in PRESETS:
                raise ValueError(f"Unknown preset '{spec['preset']}'. Presets: {', '.join(PRESETS)}.")
            if rig.params.get("kind") != "humanoid":
                raise ValueError("Presets need a humanoid rig.")
            clip = PRESETS[spec["preset"]](rig, spec)
        elif "blend" in spec and {str(x.get("preset", "") if isinstance(x, dict) else x) for x in spec["blend"][:2]} == {"walk", "run"}:
            # Locomotion blends are regenerated with blended parameters so the IK footwork stays correct.
            first = spec["blend"][0] if isinstance(spec["blend"][0], str) else spec["blend"][0].get("preset")
            wgt = float(spec.get("weight", 0.5))
            clip = preset_locomotion(rig, speed=float(spec.get("speed", 1.0)), fps=int(spec.get("fps", 30)),
                                     run_amount=wgt if first == "walk" else 1 - wgt)
        elif "blend" in spec:
            a, b = make_clips(rig, spec["blend"][:2])
            clip = blend(a, b, float(spec.get("weight", 0.5)), spec.get("name", ""))
        elif "keys" in spec:
            clip = from_keys(rig, spec.get("name", "custom"), float(spec.get("length", 1.0)), spec["keys"], int(spec.get("fps", 30)),
                             bool(spec.get("loop", True)), spec.get("root_keys"), spec.get("interp", "smooth"))
        else:
            raise ValueError("Each clip needs 'preset', 'keys' or 'blend'.")
        if spec.get("retime"):
            clip = retime(clip, float(spec["retime"]))
        if spec.get("mirror"):
            clip = mirror_clip(rig, clip)
        if spec.get("name"):
            clip.name = spec["name"]
        clips.append(clip)
    return clips


def finish_rig(rig: Rig, clips: list[Clip]) -> dict[str, Any]:
    from .studio import out_path, verdict, write_manifest
    rig_json = save_rig(rig)
    analyses = [analyse(rig, c) for c in clips]
    glb = out_path("mesh", rig.name, ".glb")
    export_glb(rig, clips, glb)
    rest = np.zeros((len(rig.bones), 4))
    rest[:, 3] = 1
    mesh_check = M.validate(pose_mesh(rig, rest))
    mesh_check.pop("boundary")
    mesh_check.pop("_split")
    sheet, gifs = preview(rig, clips, rig.name, analyses)
    issues = list(mesh_check["issues"]) + [dict(i, message=f"{c.name}: {i['message']}") for c, a in zip(clips, analyses) for i in a["issues"]]
    info = {"kind": "rig", "name": rig.name, "files": [str(glb), str(rig_json)] + [str(g) for g in gifs], "preview": str(sheet),
            "bones": [b.name for b in rig.bones], "clips": {c.name: a["metrics"] for c, a in zip(clips, analyses)},
            "mesh": mesh_check["stats"], "issues": issues, "verdict": verdict(issues)}
    write_manifest(glb, {k: v for k, v in info.items()})
    return info


def _character_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    rig = build_humanoid(safe_name(args.get("name", "character")), float(args.get("height", 1.8)), args.get("build", "normal"),
                         args.get("style", "blocky"), args.get("colors"), args.get("hair", "short"), args.get("accessory", ""))
    clips = make_clips(rig, args.get("clips") if args.get("clips") is not None else ["idle", "walk", "run", "jump"])
    return finish_rig(rig, clips)


def _animate_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import safe_name
    rig = load_rig(args["rig"])
    if args.get("name"):
        rig.name = safe_name(args["name"])
    return finish_rig(rig, make_clips(rig, args.get("clips") or []))


def _autorig_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import resolve_input, safe_name
    src = resolve_input(args["source"])
    rig = auto_rig(M.load(src), safe_name(args.get("name") or src.stem + "_rigged"))
    info = finish_rig(rig, make_clips(rig, args.get("clips") or ["idle", "walk"]))
    info["note"] = "Automatic weights: check the preview frames for stretching at the shoulders and hips."
    return info


def _object_cap(args: dict[str, Any]) -> dict[str, Any]:
    from .studio import make_sheet, out_path, resolve_input, safe_name, save_sheet, verdict, write_manifest
    src = resolve_input(args["source"])
    m = M.load(src)
    name = safe_name(args.get("name") or src.stem + "_anim")
    preset = args.get("preset")
    length = float(args.get("length", 2.0))
    if preset == "spin":
        keys = [{"t": length * i / 4, "rot": [0, 90 * i, 0], "ease": "linear"} for i in range(5)]
    elif preset == "bob":
        amp = float(args.get("amount", 0.15))
        keys = [{"t": 0, "pos": [0, 0, 0]}, {"t": length / 2, "pos": [0, amp, 0]}, {"t": length, "pos": [0, 0, 0]}]
    elif preset == "hinge":
        ang = float(args.get("amount", 100))
        keys = [{"t": 0, "rot": [0, 0, 0]}, {"t": length * 0.6, "rot": [0, ang, 0], "ease": "out"}, {"t": length, "rot": [0, ang, 0]}]
    elif preset == "pulse":
        keys = [{"t": 0, "scale": 1.0}, {"t": length / 2, "scale": float(args.get("amount", 1.15))}, {"t": length, "scale": 1.0}]
    else:
        keys = args.get("keys") or []
        if not keys:
            raise ValueError("Give a preset (spin, bob, hinge, pulse) or keys [{t, pos, rot, scale, ease}].")
    loop = bool(args.get("loop", preset != "hinge"))
    glb = out_path("mesh", name, ".glb")
    files, trs = export_object_glb(m, keys, length, loop, glb, int(args.get("fps", 30)), name)
    issues = []
    if loop:
        seam = max(float(np.abs(trs[0, :3] - trs[-1, :3]).max()), float(np.abs(trs[0, 7:] - trs[-1, 7:]).max()),
                   math.radians(q_angle(trs[0, 3:7], trs[-1, 3:7])))
        if seam > 1e-3:
            issues.append({"severity": "error", "message": f"Loop doesn't seal (first and last frame differ by {seam:.3f})."})
    panels = []
    for f in np.linspace(0, len(trs) - 1, 6).astype(int):
        pos, q, s = trs[f, :3], trs[f, 3:7], trs[f, 7:]
        mm = m.copy()
        mm.V = (mm.V * s) @ q_to_matrix(q).T + pos
        panels.append((f"frame {f}", M.render(mm, 200, 30, 15, fit_bounds=(m.bounds()[0] - 0.5, m.bounds()[1] + 0.5))))
    sheet = save_sheet(make_sheet(f"Object animation: {name}", panels, [f"{len(trs)} frames, loop {loop}"], issues, panel_size=200, cols=6), "rig", name)
    info = {"files": [str(f) for f in files], "preview": str(sheet), "frames": len(trs), "issues": issues, "verdict": verdict(issues)}
    write_manifest(glb, info)
    return info


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                return {"status": "ok", "summary": f"{summary} Verdict: {data.get('verdict')}.", "data": data}
            except (ValueError, KeyError, FileNotFoundError, PermissionError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    clip_doc = ("clips: ['idle','walk','run','jump','wave','attack'] or [{preset, speed, fps, style, retime, mirror, name}] | "
                "[{name, length, loop, keys: {Bone: [{t, rot:[x,y,z], ease}]}, root_keys}] | [{blend: [a, b], weight}]")
    reg.register(CapabilitySpec(
        id="rig.character", area="animation",
        summary="Make a rigged, animated R15-style character (blocky or rounded, colours, hair, build) with idle/walk/run/jump "
                "and more. Exports a skinned GLB with every clip, a GIF per clip, and a preview sheet with motion metrics.",
        input_schema={"name": "optional str", "height": "optional float", "build": "optional str (slim|normal|broad|chibi)",
                      "style": "optional str (blocky|rounded)", "colors": "optional dict", "hair": "optional str (short|long|none)",
                      "accessory": "optional str (hat)", "clips": "optional list: " + clip_doc},
        permission="read", handler=wrap(_character_cap, "Character built."), tags=["animation", "rig", "character", "3d", "roblox"],
    ))
    reg.register(CapabilitySpec(
        id="rig.animate", area="animation",
        summary="Add or replace clips on a saved rig (.rig.json): presets, custom keyframes with easing, blends, retiming, mirroring.",
        input_schema={"rig": "str", "clips": "list: " + clip_doc, "name": "optional str"},
        permission="read", handler=wrap(_animate_cap, "Animation built."), tags=["animation", "keyframes", "3d"],
    ))
    reg.register(CapabilitySpec(
        id="rig.auto_rig", area="animation",
        summary="Rig any standing humanoid mesh (OBJ/STL/GLB, facing +Z, feet at y=0) with a fitted R15 skeleton and automatic weights.",
        input_schema={"source": "str", "name": "optional str", "clips": "optional list"},
        permission="read", handler=wrap(_autorig_cap, "Mesh rigged."), tags=["animation", "rig", "skinning", "3d"],
    ))
    reg.register(CapabilitySpec(
        id="rig.object_anim", area="animation",
        summary="Animate a whole prop without a skeleton (spin, bob, hinge, pulse, or keys with pos/rot/scale + easing). Exports GLB.",
        input_schema={"source": "str", "preset": "optional str", "keys": "optional list", "length": "optional float",
                      "amount": "optional float", "loop": "optional bool", "name": "optional str"},
        permission="read", handler=wrap(_object_cap, "Object animated."), tags=["animation", "props", "3d"],
    ))
