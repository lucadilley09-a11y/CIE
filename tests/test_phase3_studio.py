"""Phase 3 tests: the creative studio (3D, rigs/animation, 2D/pixel, audio, review, place).

Every test drives capabilities through the registry, the way Claude calls them, and checks
both that files are made and that the quality checks hold (not just that a file exists)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "home" / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    root = tmp_path / "game"
    (root / "assets").mkdir(parents=True)
    (root / "main.js").write_text("console.log('hi')\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(root), "game")
    yield reg, root
    storage._DEFAULT_STORAGE = None


def ok(res: dict) -> dict:
    assert res["status"] == "ok", res.get("summary")
    return res["data"]


# --------------------------------------------------------------------- 3D

def test_primitives_are_closed_and_point_outward() -> None:
    from cie import mesh3d as M
    for name, fn in M.PRIMITIVES.items():
        m = fn({}, M.make_material("default"))
        rep = M.validate(m)
        if name == "plane":
            assert rep["stats"]["holes"] == 1
            continue
        assert rep["stats"]["watertight"], name
        assert rep["stats"]["volume_m3"] > 0, name
        assert rep["stats"]["inconsistent_winding_edges"] == 0, name


def test_edit_ops_keep_meshes_valid() -> None:
    from cie import mesh3d as M
    box = M.box()
    ext = M.extrude(box, {"normal": [0, 1, 0]}, 0.5, 0.5)
    assert M.validate(ext)["verdict"] == "pass" and abs(M.validate(ext)["stats"]["volume_m3"] - (1 + 0.5 * (1 + 0.25 + 0.5) / 3)) < 1e-4
    point = M.extrude(box, {"normal": [0, 1, 0]}, 0.5, 0.0)  # pyramid cap collapses to one vertex
    assert M.validate(point)["stats"]["watertight"] and M.validate(point)["stats"]["loose_vertices"] == 0
    loop = M.subdivide(M.box(), True, 2)
    assert len(loop.F) == 12 * 16 and M.validate(loop)["stats"]["watertight"]
    mir = M.mirror(M.transform(M.box(), (0.5, 0, 0)), "x")  # touching the plane: seam faces removed
    st = M.validate(mir)["stats"]
    assert st["watertight"] and abs(st["volume_m3"] - 2.0) < 1e-6
    dec = M.decimate(M.uv_sphere(0.5, 48, 24), 300)
    assert len(dec.F) <= 320 and M.validate(dec)["stats"]["watertight"]


def test_validation_catches_real_defects() -> None:
    from cie import mesh3d as M
    m = M.box()
    holed = M.Mesh(m.V, m.F[:-2], m.face_mat[:-2], m.materials)
    assert M.validate(holed)["stats"]["holes"] == 1
    flipped = M.flip(M.box())
    rep = M.validate(flipped)
    assert rep["verdict"] == "fail" and any("inward" in i["message"] for i in rep["issues"])
    assert M.validate(M.orient_outward(flipped))["verdict"] == "pass"
    dup = M.merge([M.box(), M.box()])
    assert any("duplicate" in i["message"] for i in M.validate(M.weld(dup))["issues"])
    dark_metal = M.make_material("m", {"base_color": "#202020", "metallic": 1.0})
    assert M.check_material(dark_metal)


def test_uv_unwrap_has_no_stretch_and_box_uv_is_not_mirrored() -> None:
    from cie import mesh3d as M
    u = M.uv_unwrap(M.icosphere(0.5, 2))
    st = M.uv_stats(u)
    assert st["max_stretch"] < 1.3 and st["degenerate_uv_faces"] == 0
    assert (u.UV >= -1e-9).all() and (u.UV <= 1 + 1e-9).all()
    b = M.uv_stats(M.uv_box(M.box()))
    assert b["max_stretch"] == 1.0 and b["mirrored_uv_faces"] == 0


def test_mesh_build_exports_round_trip(env) -> None:
    reg, _ = env
    spec = {"name": "crate", "materials": {"wood": "planks", "metal": {"preset": "iron"}},
            "parts": [{"shape": "rounded_box", "size": [1, 1, 1], "radius": 0.05, "position": [0, 0.5, 0], "material": "wood"},
                      {"shape": "box", "size": [1.04, 0.08, 1.04], "position": [0, 0.9, 0], "material": "metal"}],
            "uv": "unwrap", "texture_size": 128, "formats": ["glb", "obj", "stl", "gltf"]}
    d = ok(reg.run("mesh.build", spec))
    assert d["verdict"] == "pass" and Path(d["preview"]).exists()
    from cie import mesh3d as M
    for f in d["files"]:
        if f.endswith((".glb", ".gltf", ".obj", ".stl")):
            back = M.load(f)
            assert len(back.F) == d["stats"]["triangles"], f
            assert M.validate(back)["stats"]["watertight"], f
    glb = next(f for f in d["files"] if f.endswith(".glb"))
    doc, _ = M.load_gltf_doc(Path(glb))
    assert doc["asset"]["version"] == "2.0" and doc.get("images")  # baked texture embedded
    assert Path(glb + ".cie.json").exists()


def test_every_generator_passes_its_own_checks(env) -> None:
    reg, _ = env
    from cie.lowpoly import GENERATORS
    for kind in GENERATORS:
        d = ok(reg.run("mesh.generate", {"kind": kind, "params": {"seed": 2}}))
        assert d["verdict"] == "pass", (kind, d["issues"])
        assert d["spec"]["parts"], kind
    # the returned spec rebuilds the same model
    d = ok(reg.run("mesh.generate", {"kind": "tree", "params": {"seed": 5, "style": "pine"}}))
    again = ok(reg.run("mesh.build", {**d["spec"], "name": "tree_again"}))
    assert again["stats"]["triangles"] == d["stats"]["triangles"]


def test_mesh_clean_fixes_a_broken_file(env) -> None:
    reg, root = env
    from cie import mesh3d as M
    bad = M.merge([M.flip(M.box()), M.box()])
    M.write_obj(bad, root / "assets" / "bad.obj")
    d = ok(reg.run("mesh.clean", {"source": "assets/bad.obj"}))
    assert d["before"]["duplicate_faces"] > 0
    assert d["stats"]["duplicate_faces"] == 0 and d["stats"]["inconsistent_winding_edges"] == 0


# ----------------------------------------------------------------- animation

def test_character_clips_meet_motion_quality(env) -> None:
    reg, _ = env
    d = ok(reg.run("rig.character", {"name": "hero", "clips": ["idle", "walk", "run", "jump", "wave", "attack",
                                                                {"blend": ["walk", "run"], "weight": 0.5, "name": "jog"}]}))
    assert d["verdict"] == "pass", d["issues"]
    clips = d["clips"]
    for name in ("idle", "walk", "run", "jog"):
        assert clips[name]["loop_seam_deg"] < 0.5
        assert clips[name]["foot_slide_cm"] < 0.5, name
        assert clips[name]["pops"] == 0
    assert 0.9 < clips["walk"]["ground_speed_m_s"] < clips["jog"]["ground_speed_m_s"] < clips["run"]["ground_speed_m_s"]
    for name in ("jump", "attack", "wave"):
        assert clips[name]["pops"] == 0 and clips[name]["ground_penetration_cm"] < 1
    from cie import mesh3d as M
    glb = Path(d["files"][0])
    doc, _ = M.load_gltf_doc(glb)
    assert len(doc["skins"][0]["joints"]) == 16
    assert {a["name"] for a in doc["animations"]} == {"idle", "walk", "run", "jump", "wave", "attack", "jog"}
    assert any(f.endswith(".gif") for f in d["files"])


def test_metrics_catch_bad_animation() -> None:
    from cie import rig as R
    rig = R.build_humanoid()
    # a loop whose last key doesn't return to the first pose
    bad = R.from_keys(rig, "bad", 1.0, {"RightUpperArm": [{"t": 0, "rot": [0, 0, 0]}, {"t": 1.0, "rot": [60, 0, 0]}]}, loop=True)
    # force the open loop: keys end away from the start
    bad.rots[-1] = R.q_from_euler([60, 0, 0])
    a = R.analyse(rig, bad)
    assert a["verdict"] == "fail" and any("Loop" in i["message"] for i in a["issues"])
    snap = R.from_keys(rig, "snap", 1.0, {"RightUpperArm": [{"t": 0, "rot": [0, 0, 0]}, {"t": 0.5, "rot": [0, 0, 0], "ease": "step"},
                                                              {"t": 0.53, "rot": [170, 0, 0], "ease": "linear"}, {"t": 1.0, "rot": [170, 0, 0]}]},
                       loop=False, interp="eased")
    assert R.analyse(rig, snap)["metrics"]["pops"] > 0


def test_animate_saved_rig_and_object_animation(env) -> None:
    reg, _ = env
    d = ok(reg.run("rig.character", {"name": "npc", "clips": ["idle"]}))
    rig_json = next(f for f in d["files"] if f.endswith(".rig.json"))
    custom = {"name": "nod", "length": 1.0, "loop": True,
              "keys": {"Head": [{"t": 0, "rot": [0, 0, 0]}, {"t": 0.5, "rot": [15, 0, 0]}, {"t": 1.0, "rot": [0, 0, 0]}]}}
    d2 = ok(reg.run("rig.animate", {"rig": rig_json, "clips": [custom, {"preset": "walk", "retime": 2.0}, {"preset": "wave", "mirror": True}]}))
    assert d2["clips"]["nod"]["loop_seam_deg"] < 0.5
    crate = ok(reg.run("mesh.generate", {"kind": "crate"}))
    o = ok(reg.run("rig.object_anim", {"source": crate["files"][0], "preset": "spin"}))
    assert o["verdict"] == "pass"


def test_auto_rig_any_mesh(env) -> None:
    reg, root = env
    from cie import mesh3d as M
    from cie import rig as R
    m = R.build_humanoid().mesh.copy()
    m.joints = m.weights = None
    M.write_obj(m, root / "assets" / "statue.obj")
    d = ok(reg.run("rig.auto_rig", {"source": "assets/statue.obj"}))
    assert d["verdict"] == "pass" and d["clips"]["walk"]["foot_slide_cm"] < 0.5


# ------------------------------------------------------------------------ 2D

def test_oklch_round_trip_and_ramps() -> None:
    from cie import pixel as P
    for c in ("#e0b18f", "#3b6fd1", "#5f9e3a", "#101010", "#f2f2f2"):
        L, C, H = P.rgb_to_oklch(P.parse_color(c))
        assert P.hexc(P.oklch_to_rgb(L, C, H)) == c
    r = P.ramp("#3b6fd1", 5)
    Ls = [P.rgb_to_oklch(P.parse_color(x))[0] for x in r]
    assert Ls == sorted(Ls)  # dark to light
    hues = [P.rgb_to_oklch(P.parse_color(x))[2] for x in r]
    assert abs(((hues[-1] - hues[0] + 180) % 360) - 180) > 5  # hue shifted


def test_palette_and_draw(env) -> None:
    reg, _ = env
    p = ok(reg.run("pixel.palette", {"base": "#d64545", "scheme": "triadic", "name": "t"}))
    assert any(f.endswith(".gpl") for f in p["files"]) and len(p["palette"]) >= 15
    from cie.pixel import ramp
    d = ok(reg.run("pixel.draw", {"width": 24, "height": 24, "palette": ramp("#d64545", 5), "name": "heart", "ops": [
        {"op": "symmetry", "axis": "x"},
        {"op": "polygon", "points": [[12, 21], [3, 12], [3, 7], [6, 4], [9, 4], [12, 7]], "color": "p2"},
        {"op": "shade", "ramp": ["p0", "p1", "p2", "p3", "p4"]}, {"op": "symmetry", "axis": ""},
        {"op": "outline", "color": "#1a1420"}]}))
    assert d["verdict"] == "pass", d["issues"]
    px = np.array(Image.open(d["files"][0]))
    assert (px[..., 3] == px[:, ::-1, 3]).all()  # mirrored silhouette (shading is lit from one side on purpose)
    assert set(np.unique(px[..., 3])) <= {0, 255}


def test_pixel_qa_catches_problems() -> None:
    from cie import pixel as P
    px = np.zeros((16, 16, 4), np.uint8)
    px[4:12, 4:12] = (200, 50, 50, 255)
    px[1, 1] = (255, 255, 255, 255)     # stray pixel
    px[4, 4, 3] = 128                   # soft alpha
    a = P.analyse_image(px, pixel_art=True)
    msgs = " ".join(i["message"] for i in a["issues"])
    assert "stray" in msgs and "semi-transparent" in msgs
    tile = np.zeros((16, 16, 4), np.uint8)
    tile[..., 3] = 255
    tile[..., :3] = np.linspace(20, 230, 16).astype(np.uint8)[None, :, None]  # smooth inside, hard jump at the wrap
    assert P.analyse_image(tile, True, tileable=True)["metrics"]["seam_ratio"] > 1.25


def test_sprite_sheet_from_3d_is_consistent(env) -> None:
    reg, _ = env
    d = ok(reg.run("pixel.sprite_from_3d", {"character": {"colors": {"shirt": "#c8332f"}}, "clip": "walk", "size": 32, "frames": 6, "name": "hero"}))
    assert d["verdict"] == "pass", d["issues"]
    sheet = np.array(Image.open(d["files"][0]))
    assert sheet.shape[:2] == (32 * 8, 32 * 6)
    meta = json.loads(Path(d["files"][1]).read_text())
    assert len(meta["frames"]) == 48 and len(meta["meta"]["frameTags"]) == 8
    assert d["metrics"]["baseline_spread_px"] <= 4


def test_textures_tile_and_tileset_is_seamless(env) -> None:
    reg, _ = env
    from cie.pixel import TERRAIN
    for kind in TERRAIN:
        d = ok(reg.run("pixel.texture", {"kind": kind, "size": 32, "seed": 3}))
        assert d["verdict"] == "pass", (kind, d["issues"])
    t = ok(reg.run("pixel.tileset", {"upper": "grass", "lower": "dirt", "size": 16}))
    sheet = np.array(Image.open(t["files"][0]))
    s = 16
    tile = lambda i: sheet[(i // 4) * s:(i // 4 + 1) * s, (i % 4) * s:(i % 4 + 1) * s]  # noqa: E731
    # tiles that share an edge match pixel-for-pixel along it: index 15 (all upper) next to 15, and 0 next to 0
    for i in range(16):
        right_edge = tile(i)[:, -1]
        for j in range(16):
            left_corners_i = ((i >> 2) & 1, i & 1)        # TR, BR of tile i
            left_corners_j = ((j >> 3) & 1, (j >> 1) & 1)  # TL, BL of tile j
            if left_corners_i == left_corners_j:
                # same corner membership along the shared edge -> same terrain choice along it
                assert ((right_edge[:, 3] > 0) == (tile(j)[:, 0, 3] > 0)).all()


def test_animate_sheet_ui_vector_and_edit(env) -> None:
    reg, root = env
    heart = ok(reg.run("pixel.draw", {"width": 16, "height": 16, "name": "dot", "ops": [{"op": "ellipse", "from": [3, 3], "to": [12, 12], "color": "#e0b44a"}]}))
    a = ok(reg.run("pixel.animate", {"source": heart["files"][0], "effect": "bob", "frames": 6, "name": "dot_bob"}))
    assert a["frames"] == 6 and any(f.endswith(".gif") for f in a["files"])
    sh = ok(reg.run("pixel.sheet", {"frames": [heart["files"][0]] * 4, "name": "dots"}))
    assert json.loads(Path(sh["files"][1]).read_text())["meta"]["cell"] == [16, 16]
    ui = ok(reg.run("pixel.ui_kit", {"name": "ui"}))
    assert ui["verdict"] == "pass" and "slice" in ui["parts"]["panel"]
    v = ok(reg.run("image.vector", {"width": 64, "height": 64, "name": "icon", "shapes": [
        {"type": "rect", "x": 4, "y": 4, "w": 56, "h": 56, "r": 10, "fill": {"linear": ["#5ac8fa", "#3b6fd1"], "angle": 90}, "stroke": "#14101c", "stroke_width": 2},
        {"type": "circle", "cx": 32, "cy": 30, "r": 12, "fill": "#ffd36b"}]}))
    svg = Path(next(f for f in v["files"] if f.endswith(".svg"))).read_text()
    assert "<svg" in svg and "linearGradient" in svg
    # background removal on a project image (input from the project, output to the studio)
    img = np.zeros((40, 40, 4), np.uint8)
    img[...] = (240, 240, 240, 255)
    img[10:30, 10:30] = (30, 120, 200, 255)
    Image.fromarray(img).save(root / "assets" / "photo.png")
    e = ok(reg.run("image.edit", {"source": "assets/photo.png", "ops": [{"op": "remove_background"}, {"op": "trim"}]}))
    out = np.array(Image.open(e["files"][0]))
    assert out.shape[:2] == (20, 20)
    assert out[10, 10, 3] == 255 and tuple(out[10, 10, :3]) == (30, 120, 200)


# --------------------------------------------------------------------- audio

def test_every_sfx_is_clean(env) -> None:
    reg, _ = env
    from cie.synth import SFX
    for kind in SFX:
        d = ok(reg.run("sound.sfx", {"kind": kind}))
        assert d["verdict"] == "pass", (kind, d["issues"])
        m = d["metrics"]
        assert m["true_peak_dbtp"] <= -1.0 and m["clipped_samples"] == 0 and m["edge_level"] < 0.02, kind
    v = ok(reg.run("sound.sfx", {"kind": "jump", "variations": 3}))
    assert len(v["variations"]) == 3 and len({x["duration_s"] for x in v["variations"]}) > 1


def test_ambience_and_music_loop_seamlessly(env) -> None:
    reg, _ = env
    for kind in ("wind", "rain", "forest"):
        d = ok(reg.run("sound.ambience", {"kind": kind, "length": 6}))
        assert d["verdict"] == "pass", (kind, d["issues"])
    m = ok(reg.run("sound.music", {"mood": "happy", "style": "chiptune", "bars": 4, "key": "D"}))
    assert m["verdict"] == "pass", m["issues"]
    assert m["metrics"]["loop_seam_jump"] < 6
    mid = Path(m["midi"]).read_bytes()
    assert mid[:4] == b"MThd" and mid.count(b"MTrk") == 5


def test_lufs_matches_reference_tone() -> None:
    from cie import synth as A
    # BS.1770: a full-scale 997 Hz sine reads -3.01 LUFS; at -3.01 dBFS peak it reads -6.02
    x = np.sin(2 * np.pi * 997 * np.arange(A.SR * 3) / A.SR)
    assert abs(A.lufs(x) - (-3.01)) < 0.15
    assert abs(A.lufs(x * 10 ** (-3.01 / 20)) - (-6.02)) < 0.15


def test_audio_analysis_catches_problems() -> None:
    from cie import synth as A
    sr = A.SR
    clip = np.sin(2 * np.pi * 220 * np.arange(sr) / sr) * 1.4
    a = A.analyse(np.clip(clip, -1, 1))
    assert a["verdict"] == "fail" and a["metrics"]["clipped_samples"] > 0
    x = np.sin(2 * np.pi * 220 * np.arange(sr) / sr) * 0.3
    x[20000] += 0.6
    assert A.analyse(x)["metrics"]["clicks"] == 1
    square = A.fade(A.osc("square", 440, 1.0) * 0.5)
    assert A.analyse(square)["metrics"]["clicks"] == 0  # periodic edges are not clicks
    raw = np.sin(2 * np.pi * 220 * np.arange(sr) / sr) * 0.5
    assert any("click" in i["message"] for i in A.analyse(raw[:sr // 2 + 37], kind="sfx")["issues"])
    bad_loop = np.sin(2 * np.pi * 3 * np.arange(sr) / sr) * 0.5
    assert any("Loop point" in i["message"] for i in A.analyse(bad_loop[: int(sr * 0.9)], kind="ambience", loop=True)["issues"])


def test_audio_editing_ops(env) -> None:
    reg, root = env
    from cie import synth as A
    A.write_wav(root / "assets" / "boom.wav", A.make_sfx("explosion"))
    d = ok(reg.run("sound.edit", {"source": "assets/boom.wav", "ops": [
        {"op": "trim", "start": 0, "end": 1.0}, {"op": "pitch", "semitones": -3}, {"op": "eq", "low": 4, "high": -3},
        {"op": "fade_out", "seconds": 0.3}, {"op": "normalize", "lufs": -16}]}))
    assert abs(d["metrics"]["lufs"] + 16) < 0.6 and d["metrics"]["true_peak_dbtp"] <= -1.0
    x = A.make_sfx("coin")
    st = A.time_stretch(x, 0.5)
    assert abs(len(st) / len(x) - 2.0) < 0.15
    loop = ok(reg.run("sound.edit", {"source": "assets/boom.wav", "kind": "ambience", "ops": [{"op": "make_loop", "crossfade": 0.3}]}))
    assert loop["metrics"]["loop_seam_jump"] < 6
    # 24-bit and stereo round trip
    p = A.write_wav(root / "assets" / "st.wav", np.stack([x, x * 0.5], 1), bits=24)
    y, sr = A.read_wav(p)
    assert y.shape[1] == 2 and sr == A.SR and np.abs(y[:, 0] - x[: len(y)]).max() < 1e-4


# ------------------------------------------------------------- studio glue

def test_studio_outputs_stay_out_of_projects_until_placed(env) -> None:
    reg, root = env
    before = {p.name for p in root.rglob("*")}
    d = ok(reg.run("mesh.generate", {"kind": "barrel", "formats": ["obj"]}))
    assert {p.name for p in root.rglob("*")} == before  # nothing written to the project
    place = reg.run("studio.place", {"source": d["files"][0], "dest": "assets/models"})
    assert place["status"] == "approval_required"  # write needs the owner
    res = reg.run("studio.place", {"source": d["files"][0], "dest": "assets/models"}, approved=True)
    assert res["status"] == "ok"
    copied = [Path(p).name for p in res["data"]["copied"]]
    assert "barrel.obj" in copied and "barrel.mtl" in copied
    assert any("auto_snapshot" in e for e in res.get("evidence", []))
    again = reg.run("studio.place", {"source": d["files"][0], "dest": "assets/models"}, approved=True)
    assert again["status"] == "error"  # no silent overwrite


def test_review_any_asset_and_view(env) -> None:
    reg, root = env
    from cie import studio
    from cie import synth as A
    A.write_wav(root / "assets" / "beep.wav", A.make_sfx("blip"))
    Image.fromarray(np.full((8, 8, 4), 255, np.uint8)).save(root / "assets" / "w.png")
    from cie import mesh3d as M
    M.write_stl(M.box(), root / "assets" / "b.stl")
    for f in ("assets/beep.wav", "assets/w.png", "assets/b.stl"):
        d = ok(reg.run("studio.review", {"path": f}))
        assert Path(d["preview"]).exists()
        data, info = studio.load_view(d["preview"], 512)
        assert data[:8] == b"\x89PNG\r\n\x1a\n" and max(info["shown_size"]) <= 512
    with pytest.raises(PermissionError):
        studio.load_view(str(Path(root).parent / "elsewhere.png"))
    listing = ok(reg.run("studio.list", {}))
    assert listing["count"] > 0


def test_creative_tools_are_read_only_to_projects(env) -> None:
    reg, _ = env
    creative = [c for c in reg._caps.values() if c.area in ("3d", "animation", "2d") or c.id.startswith(("sound.", "studio."))]
    assert len(creative) >= 25
    for c in creative:
        expected = "write" if c.id == "studio.place" else "read"
        assert c.permission == expected, c.id
