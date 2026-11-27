"""Phase 4 tests: workflows (approval once per run, retries, quality gates, rollback, foreach,
conditions), browser-game checks/packaging, and the Blender integration's host side.

Blender itself isn't installed in CI, so the Blender tests swap in a stand-in executable that
speaks the same job protocol (job.json in, result.json out). scripts/blender_smoke.py runs the
real thing on a PC with Blender."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_BLENDER = r'''
import json, shutil, sys
from pathlib import Path
sys.path.insert(0, ROOT)
job_dir = Path(sys.argv[-1])
job = json.loads((job_dir / "job.json").read_text())
p = job["params"]
from PIL import Image
def render(stem, views):
    out = []
    for v in views:
        path = job_dir / f"{stem}_{v['name']}.png"
        Image.new("RGBA", (32, 32), (200, 120, 80, 255)).save(path)
        out.append({"path": str(path), "engine": "eevee"})
    return out
if job["op"] == "info":
    data = {"version": "4.2.0", "python": "3.11", "engines": ["BLENDER_EEVEE_NEXT", "CYCLES"], "gltf": True, "fbx": True}
elif job["op"] == "process":
    if any(o["op"] == "boom" for o in p["ops"]):
        raise SystemExit(3)
    files = []
    for fmt in p["formats"]:
        dst = job_dir / f"{p['stem']}.{fmt}"
        shutil.copy(p["input"], dst)
        files.append(str(dst))
    data = {"before": {"triangles": 12}, "after": {"triangles": 48}, "applied": [o["op"] for o in p["ops"]], "files": files}
    if p.get("render"):
        data["renders"] = render(p["stem"], p["render"]["views"])
elif job["op"] == "render":
    data = {"renders": render(p["stem"], p["views"]), "stats": {}}
elif job["op"] == "bake":
    maps = {}
    for m in p["maps"]:
        path = job_dir / f"{p['stem']}_{m}.png"
        Image.new("RGB", (16, 16), (128, 128, 255)).save(path)
        maps[m] = str(path)
    dst = job_dir / f"{p['stem']}.glb"
    shutil.copy(p["low"], dst)
    data = {"maps": maps, "files": [str(dst)], "renders": render(p["stem"], p["render"]["views"]) if p.get("render") else []}
(job_dir / "result.json").write_text(json.dumps({"ok": True, "data": data, "log": ["fake blender"], "seconds": 0.1}))
'''


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "home" / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    root = tmp_path / "hearth"
    (root / "games").mkdir(parents=True)
    (root / "sdk").mkdir()
    (root / "sdk" / "hearth-connect.js").write_text("window.Hearth={unlock(){}};\n", encoding="utf-8")
    (root / "keep.txt").write_text("original\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(root), "hearth")
    yield reg, root
    storage._DEFAULT_STORAGE = None


@pytest.fixture()
def fake_blender(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cie import blender
    script = tmp_path / "fake_blender.py"
    script.write_text(f"ROOT = {str(ROOT)!r}\n" + FAKE_BLENDER, encoding="utf-8")
    monkeypatch.setattr(blender, "find_blender", lambda: sys.executable)
    monkeypatch.setattr(blender, "_argv", lambda exe, job_dir: [sys.executable, str(script), str(job_dir)])
    return env


def ok(res: dict) -> dict:
    assert res["status"] == "ok", res.get("summary")
    return res["data"]


def approve(run_id: str) -> None:
    from cie.__main__ import main
    assert main(["approve", run_id, "--yes"]) == 0


# ----------------------------------------------------------------- workflows

def test_read_only_workflow_runs_straight_through(env) -> None:
    reg, root = env
    wf = {"name": "t", "inputs": {"kind": {"default": "rock"}},
          "steps": [{"id": "a", "capability": "mesh.generate", "args": {"kind": "$inputs.kind", "params": {"seed": 1}}, "require": "pass"},
                    {"id": "b", "capability": "studio.review", "args": {"path": "$steps.a.data.files.0"}},
                    {"id": "c", "capability": "mesh.generate", "when": "$steps.b.data.verdict == \"fail\"", "args": {"kind": "rock"}}]}
    d = ok(reg.run("flow.run", {"workflow": wf}))
    assert d["status"] == "done"
    st = {s["id"]: s["status"] for s in d["steps"]}
    assert st == {"a": "ok", "b": "ok", "c": "skipped"}
    assert Path(d["outputs"]["b"]["preview"]).exists()


def test_changes_need_owner_approval_once_and_exact_plan(env) -> None:
    reg, root = env
    items = [{"capability": "mesh.generate", "args": {"kind": "rock", "params": {"seed": 1}}},
             {"capability": "sound.sfx", "args": {"kind": "coin"}}]
    res = reg.run("flow.run", {"workflow": "asset_pack", "inputs": {"items": items, "dest": "assets"}})
    assert res["status"] == "approval_required"
    run_id = res["data"]["run_id"]
    assert [c["capability"] for c in res["data"]["changes"]] == ["studio.place"]
    assert not (root / "assets").exists()
    with pytest.raises(PermissionError):
        from cie import flows
        flows.resume(reg, run_id)
    blocked = reg.run("flow.resume", {"run_id": run_id})
    assert blocked["status"] == "error" and "approve" in blocked["summary"]
    approve(run_id)
    d = ok(reg.run("flow.resume", {"run_id": run_id}))
    assert d["status"] == "done", d
    placed = sorted(p.name for p in (root / "assets").iterdir())
    assert any(n.startswith("rock") and n.endswith(".glb") for n in placed) and "coin.wav" in placed
    again = reg.run("flow.resume", {"run_id": run_id})
    assert again["status"] == "error"  # a finished run can't be replayed


def test_tampered_plan_invalidates_approval(env) -> None:
    reg, _ = env
    from cie import flows
    res = reg.run("flow.run", {"workflow": "asset_pack", "inputs": {"items": [{"capability": "sound.sfx", "args": {"kind": "blip"}}], "dest": "assets"}})
    run_id = res["data"]["run_id"]
    approve(run_id)
    run = flows.load_run(run_id)
    run["inputs"]["dest"] = "elsewhere"
    run["status"] = "approved"
    flows._save_run(run)
    with pytest.raises(PermissionError):
        flows.resume(reg, run_id)


def test_failure_rolls_back_every_change(env) -> None:
    reg, root = env
    wf = {"name": "rb", "steps": [
        {"id": "make", "capability": "sound.sfx", "args": {"kind": "coin"}},
        {"id": "place", "capability": "studio.place", "args": {"source": "$steps.make.data.files.0", "dest": "assets/sfx"}},
        {"id": "boom", "capability": "game.check", "args": {"path": "keep.txt"}, "retry": 1}]}
    res = reg.run("flow.run", {"workflow": wf})
    approve(res["data"]["run_id"])
    d = reg.run("flow.resume", {"run_id": res["data"]["run_id"]})
    assert d["data"]["status"] == "rolled_back" and d["data"]["failed_step"] == "boom"
    assert not (root / "assets" / "sfx" / "coin.wav").exists()
    assert (root / "keep.txt").read_text() == "original\n"
    boom = next(s for s in d["data"]["steps"] if s["id"] == "boom")
    assert boom["attempts"] == 2  # retried once before giving up


def test_quality_gate_retries_with_a_new_seed(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reg, _ = env
    from cie import lowpoly
    real = lowpoly._generate_cap
    seen = []

    def flaky(args):
        seen.append(args.get("params", {}).get("seed"))
        out = real(args)
        if len(seen) == 1:
            out["verdict"] = "fail"
        return out

    monkeypatch.setattr(lowpoly, "_generate_cap", flaky)
    reg2 = __import__("cie.registry", fromlist=["x"]).CapabilityRegistry()
    __import__("cie.registry", fromlist=["x"]).register_all_capabilities(reg2)
    d = ok(reg2.run("flow.run", {"workflow": {"name": "q", "steps": [
        {"id": "g", "capability": "mesh.generate", "args": {"kind": "rock", "params": {"seed": 4}}, "require": "pass", "retry": 2, "vary": "params.seed"}]}}))
    assert d["status"] == "done" and seen == [4, 5]


def test_validation_and_library(env) -> None:
    reg, _ = env
    bad = {"name": "x", "steps": [{"id": "a", "capability": "nope.nothing"},
                                  {"id": "b", "capability": "game.check", "args": {"path": "$steps.zzz.data.x"}},
                                  {"id": "c", "capability": "flow.run"}]}
    probs = ok(reg.run("flow.validate", {"workflow": bad}))["problems"]
    assert len(probs) == 3
    good = {"name": "my_rocks", "description": "rocks", "inputs": {"n": {"default": 1}},
            "steps": [{"id": "a", "capability": "mesh.generate", "args": {"kind": "rock"}}]}
    ok(reg.run("flow.save", {"workflow": good}))
    names = [w["name"] for w in ok(reg.run("flow.list", {}))["workflows"]]
    assert {"blender_polish", "asset_pack", "bug_hunt", "release_prep", "new_browser_game", "my_rocks"} <= set(names)
    assert ok(reg.run("flow.run", {"workflow": "my_rocks"}))["status"] == "done"
    hist = ok(reg.run("flow.history", {}))["runs"]
    assert hist and hist[0]["name"] == "my_rocks"
    assert reg.run("flow.save", {"workflow": {**good, "name": "asset_pack"}})["status"] == "error"


def test_new_browser_game_end_to_end(env) -> None:
    reg, root = env
    res = reg.run("flow.run", {"workflow": "new_browser_game", "inputs": {
        "project_root": str(root), "game_id": "blob-run", "title": "Blob Run",
        "assets": [{"capability": "pixel.texture", "args": {"kind": "grass", "size": 16}},
                   {"capability": "sound.sfx", "args": {"kind": "jump"}}]}})
    assert res["status"] == "approval_required"
    caps = [c["capability"] for c in res["data"]["changes"]]
    assert caps.count("templates.apply") == 1 and caps.count("studio.place") >= 1  # placed per asset, decided during the run
    approve(res["data"]["run_id"])
    d = ok(reg.run("flow.resume", {"run_id": res["data"]["run_id"]}))
    assert d["status"] == "done", d["steps"]
    game = root / "games" / "blob-run"
    assert (game / "index.html").exists() and (game / "assets" / "jump.wav").exists()
    z = Path(d["outputs"]["zip"]["zip"])
    with zipfile.ZipFile(z) as zf:
        assert "blob-run/index.html" in zf.namelist()


# ------------------------------------------------------------------ game.*

def test_game_check_finds_release_problems(env) -> None:
    reg, root = env
    g = root / "games" / "demo"
    (g / "assets").mkdir(parents=True)
    (g / "assets" / "Hero.png").write_bytes(b"\x89PNG")
    (g / "index.html").write_text('<html><head><meta charset="utf-8"><script src="../../sdk/hearth-connect.js"></script></head>'
                                  '<body><img src="assets/hero.png"><script src="game.js"></script></body></html>', encoding="utf-8")
    (g / "game.js").write_text("Hearth.unlock('x');\nconst s = 'sounds/missing.wav';\ndebugger;\n", encoding="utf-8")
    d = ok(reg.run("game.check", {"path": "games/demo"}))
    msgs = " ".join(i["message"] for i in d["issues"])
    assert "first element" in msgs and "guarded" in msgs
    assert "Wrong letter case" in msgs and "missing.wav" in msgs and "debugger" in msgs
    assert d["verdict"] == "fail"


# --------------------------------------------------------------------- Blender

def test_blender_missing_is_reported_not_crashed(env, monkeypatch: pytest.MonkeyPatch) -> None:
    reg, _ = env
    from cie import diagnostics
    monkeypatch.setattr(diagnostics, "detect_blender", lambda cfg=None: None)
    st = ok(reg.run("blender.status", {}))
    assert st["available"] is False and "blender.org" in st["message"]
    crate = ok(reg.run("mesh.generate", {"kind": "crate"}))
    res = reg.run("blender.process", {"source": crate["files"][0], "ops": [{"op": "bevel"}]})
    assert res["status"] == "tool_missing"


def test_blender_job_protocol_process_render_bake(fake_blender) -> None:
    reg, _ = fake_blender
    st = ok(reg.run("blender.status", {"refresh": True}))
    assert st["available"] and st["version"] == "4.2.0"
    crate = ok(reg.run("mesh.generate", {"kind": "crate"}))
    d = ok(reg.run("blender.process", {"source": crate["files"][0], "formats": ["glb", "fbx"],
                                       "ops": [{"op": "bevel", "width": 0.01}, {"op": "boolean", "operation": "difference",
                                                                               "with": {"shape": "sphere", "size": 0.4, "position": [0, 1, 0]}},
                                               {"op": "material", "preset": "wood"}]}))
    assert any(f.endswith(".fbx") for f in d["files"]) and d["stats"]["triangles"] > 0
    assert Path(d["preview"]).exists() and d["ops"] == ["bevel", "boolean", "material"]
    r = ok(reg.run("blender.render", {"source": crate["files"][0], "views": "turntable"}))
    assert len([f for f in r["files"] if f.endswith(".png")]) == 4
    b = ok(reg.run("blender.bake", {"source": crate["files"][0], "maps": ["normal", "ao"], "high_ops": [{"op": "bevel"}]}))
    assert b["maps"] == ["normal", "ao"] and any("_normal" in f for f in b["files"])


def test_blender_rejects_bad_ops_before_starting(fake_blender) -> None:
    reg, _ = fake_blender
    crate = ok(reg.run("mesh.generate", {"kind": "crate"}))
    res = reg.run("blender.process", {"source": crate["files"][0], "ops": [{"op": "exec_python", "code": "import os"}]})
    assert res["status"] == "error" and "Unknown Blender op" in res["summary"]
    res = reg.run("blender.process", {"source": crate["files"][0], "ops": [{"op": "material", "preset": "lava"}]})
    assert res["status"] == "error"


def test_blender_polish_workflow_with_fake_blender(fake_blender) -> None:
    reg, _ = fake_blender
    crate = ok(reg.run("mesh.generate", {"kind": "crate"}))
    d = ok(reg.run("flow.run", {"workflow": "blender_polish", "inputs": {"source": crate["files"][0], "material": "wood"}}))
    assert d["status"] == "done", d["steps"]
    assert {s["id"]: s["status"] for s in d["steps"]} == {"detail": "ok", "bake": "ok", "render": "skipped", "place": "skipped"}


def test_job_script_has_no_dangerous_calls() -> None:
    src = (ROOT / "cie" / "blender_job.py").read_text(encoding="utf-8")
    for bad in ("exec(", "eval(", "subprocess", "os.system", "__import__"):
        assert bad not in src, bad


def test_blender_detection_prefers_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cie import diagnostics
    pf = tmp_path / "pf"
    for v in ("4.2", "5.1", "4.5"):
        exe = pf / "Blender Foundation" / f"Blender {v}" / "blender.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("x")
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)
    monkeypatch.setattr(diagnostics, "_blender_from_registry", lambda: [])  # ignore a real install on the test PC
    monkeypatch.setattr(diagnostics.Path, "home", staticmethod(lambda: tmp_path / "home"))
    cands = diagnostics.blender_candidates()
    assert "5.1" in str(cands[0])
    assert "5.1" in diagnostics.detect_blender({"blender_path": ""})


def test_rollback_removes_a_created_game(env) -> None:
    reg, root = env
    res = reg.run("flow.run", {"workflow": "new_browser_game", "inputs": {
        "project_root": str(root), "game_id": "doomed", "title": "Doomed",
        "assets": [{"capability": "mesh.generate", "args": {"kind": "not_a_kind"}}]}})
    approve(res["data"]["run_id"])
    d = reg.run("flow.resume", {"run_id": res["data"]["run_id"]})["data"]
    assert d["status"] == "rolled_back" and d["failed_step"] == "make"
    assert not (root / "games" / "doomed" / "index.html").exists()


def test_bake_checks_catch_missed_rays(tmp_path: Path) -> None:
    import numpy as np
    from PIL import Image

    from cie import blender
    from cie import mesh3d as M
    m = M.uv_unwrap(M.box())
    good, bad = tmp_path / "good.png", tmp_path / "bad.png"
    Image.new("RGB", (64, 64), (150, 100, 60)).save(good)
    arr = np.full((64, 64, 3), (150, 100, 60), np.uint8)
    arr[:, :40] = 0  # most of the islands never got hit
    Image.fromarray(arr).save(bad)
    assert not [i for i in blender.bake_checks(m, {"color": str(good)}) if "empty" in i["message"]]
    issues = blender.bake_checks(m, {"color": str(bad)})
    assert any(i["severity"] == "error" and "empty" in i["message"] for i in issues)
