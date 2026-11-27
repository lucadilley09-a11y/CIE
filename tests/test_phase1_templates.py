"""Phase 1.5 template tests: built-ins validate, validation catches real
problems, apply never overwrites and is undoable, user templates."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()  # Windows runners can hand back short/alias paths
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "home" / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    proj = tmp_path / "hearth"
    proj.mkdir()
    from cie.projects import register_project
    register_project(str(proj), "hearth")
    yield reg, proj
    storage._DEFAULT_STORAGE = None


def _ok(reg, cid, args, approved=False):
    res = reg.run(cid, args, approved=approved)
    assert res["status"] == "ok", res
    return res["data"]


def test_every_builtin_template_validates(env) -> None:
    reg, _ = env
    listed = _ok(reg, "templates.list", {})
    assert listed["count"] >= 11
    kinds = {t["kind"] for t in listed["templates"]}
    assert {"code", "ui", "2d", "3d", "material", "animation", "project", "workflow"} <= kinds
    for t in listed["templates"]:
        assert _ok(reg, "templates.validate", {"id": t["id"]})["status"] == "passed", t["id"]


def test_list_filters(env) -> None:
    reg, _ = env
    roblox = _ok(reg, "templates.list", {"engine": "roblox"})["templates"]
    assert {t["id"] for t in roblox} == {"luau-module", "luau-validated-remote"}
    assert all(t["kind"] == "3d" for t in _ok(reg, "templates.list", {"kind": "3d"})["templates"])


def test_hearth_rules_are_enforced(env, monkeypatch) -> None:
    from cie import templates
    good = templates.render("hearth-browser-game", {"game_id": "g", "title": "G"})
    assert templates._hearth_checks(good) == []
    bad = dict(good)
    html = good["games/g/index.html"].decode().replace(
        '  <script src="../../sdk/hearth-connect.js"></script>\n', "").replace(
        "<title>", '<script src="../../sdk/hearth-connect.js"></script><title>')
    bad["games/g/index.html"] = html.encode()
    bad["games/g/game.js"] = (good["games/g/game.js"].decode()
                              + "\nHearth.unlock('secret');\nindexedDB.open('x');\n").encode()
    problems = templates._hearth_checks(bad)
    assert any("first element" in p for p in problems)
    assert any("guarded" in p for p in problems)
    assert any("localStorage only" in p for p in problems)
    assert any("not listed" in p for p in problems)


def test_validation_catches_broken_files() -> None:
    from cie.templates import _check_file
    assert _check_file("m.obj", b"v 0 0 0\nf 1 2 3\n", [])  # face index out of range
    assert _check_file("x.json", b"{not json", [])
    assert _check_file("a.luau", b"local x = {", [])  # unclosed + missing --!strict
    assert _check_file("t.lua", b"print('{{name}}')", [])  # unfilled placeholder
    assert _check_file("ok.luau", b"--!strict\nlocal t = { a = (1) }\n", []) == []


def test_apply_creates_files_never_overwrites_and_is_undoable(env) -> None:
    reg, proj = env
    args = {"id": "hearth-browser-game", "target_dir": str(proj), "params": {"game_id": "dash", "title": "Dash"}}
    assert reg.run("templates.apply", args)["status"] == "approval_required"
    done = _ok(reg, "templates.apply", args, approved=True)
    index = proj / "games" / "dash" / "index.html"
    assert index.exists() and "<title>Dash</title>" in index.read_text()
    again = reg.run("templates.apply", args, approved=True)
    assert again["status"] == "error" and "already exist" in again["summary"]
    preview = _ok(reg, "storage.restore_preview", {"snapshot_id": done["snapshot_id"]})
    assert len(preview["will_delete"]) == 3
    _ok(reg, "storage.restore", {"snapshot_id": done["snapshot_id"]}, approved=True)
    assert not index.exists()


def test_apply_rejects_missing_or_unsafe_params(env) -> None:
    reg, proj = env
    miss = reg.run("templates.apply", {"id": "luau-module", "target_dir": str(proj)}, approved=True)
    assert miss["status"] == "error" and "module_name" in miss["summary"]
    unsafe = reg.run("templates.apply", {"id": "luau-module", "target_dir": str(proj),
                                         "params": {"module_name": "../../evil"}}, approved=True)
    assert unsafe["status"] == "error"


def test_generated_tileset_png(env) -> None:
    reg, proj = env
    _ok(reg, "templates.apply", {"id": "tileset-guide", "target_dir": str(proj),
                                 "params": {"tile": 32, "cols": 4, "rows": 2}}, approved=True)
    from PIL import Image
    with Image.open(proj / "tileset_guide.png") as im:
        assert im.size == (128, 64) and im.mode == "RGBA"


def test_save_user_template_with_provenance(env) -> None:
    reg, proj = env
    (proj / "snippets").mkdir()
    (proj / "snippets" / "util.luau").write_text("--!strict\nreturn { clamp = math.clamp }\n", encoding="utf-8")
    saved = _ok(reg, "templates.save", approved=True, args={"id": "my-util", "name": "Util", "kind": "code", "description": "helpers",
                                        "paths": [str(proj / "snippets" / "util.luau")], "base_dir": str(proj / "snippets"),
                                        "engines": ["roblox"]})
    assert saved["validation"] == "passed"
    shown = _ok(reg, "templates.show", {"id": "my-util"})
    assert shown["provenance"]["author"] == "user" and shown["origin"] == "user"
    assert any(t["id"] == "my-util" for t in _ok(reg, "templates.list", {"engine": "roblox"})["templates"])
    dup = reg.run("templates.save", {"id": "luau-module", "name": "x", "kind": "code", "description": "x",
                                     "paths": [], "base_dir": str(proj)}, approved=True)
    assert dup["status"] == "error"
