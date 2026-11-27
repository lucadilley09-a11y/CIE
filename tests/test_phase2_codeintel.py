"""Phase 2.1 code intelligence: masking, definitions, callers, module graph,
cycles, unused code, duplicates, complexity, and API, for Luau, JS, Java, Kotlin."""
from __future__ import annotations

from pathlib import Path

import pytest

from cie.codescan import mask, scan


def test_mask_hides_comments_and_strings_but_keeps_lines() -> None:
    lua = 'local s = "end function x()" -- function y()\n--[[ function z() end ]]\nlocal t = [[\nfunction w() end\n]]\n'
    m = mask(lua, "lua")
    assert m.count("\n") == lua.count("\n") and len(m) == len(lua)
    assert "function" not in m and "local s" in m
    js = "const a = `function q() {}`; // function r() {}\n/* class X {} */ let b = 'x';"
    mj = mask(js, "js")
    assert "function" not in mj and "class" not in mj and "let b" in mj


def test_luau_definitions_exports_and_complexity() -> None:
    src = ("local M = {}\nlocal function helper(a, b)\n  if a and b then return 1 elseif a then return 2 end\n"
           "  return 0\nend\nfunction M.spawn(p)\n  helper(1, 2)\nend\nfunction M:update(dt) end\n"
           "local tick = function() end\nreturn M\n")
    r = scan(src, "luau")
    by = {d.name: d for d in r.definitions}
    assert by["helper"].exported is False and by["helper"].complexity == 4 and by["helper"].line_end == 5
    assert by["spawn"].exported and by["spawn"].qualifier == "M"
    assert by["update"].exported and "tick" in by
    assert ("helper", 7) in r.calls


def test_java_kotlin_and_js_definitions() -> None:
    java = scan("public class Gun {\n  private static boolean reload() { return true; }\n"
                "  public void fire(String who) { if (who != null && ammo > 0) { reload(); } }\n}\n", "java")
    jb = {d.name: d for d in java.definitions}
    assert jb["Gun"].kind == "class" and jb["reload"].exported is False and jb["fire"].complexity == 3
    kt = scan("class Mod : ModInitializer {\n  override fun onInitialize() { register(\"x\") }\n"
              "  private fun register(id: String) = println(id)\n}\n", "kotlin")
    kb = {d.name: d for d in kt.definitions}
    assert kb["register"].exported is False and ("register", 2) in kt.calls
    js = scan("export function add(a, b) { return a + b; }\nconst mul = (a, b) => a * b;\n"
              "class P {\n  update(dt) {\n    if (dt) { this.x += dt; }\n  }\n}\nadd(1, mul(2, 3));\n", "javascript")
    names = {d.name: d for d in js.definitions}
    assert names["add"].exported and not names["mul"].exported and names["update"].kind == "method"
    assert {"add", "mul"} <= {c for c, _ in js.calls}


@pytest.fixture()
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "home" / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    root = tmp_path / "game"
    (root / "src" / "shared").mkdir(parents=True)
    (root / "src" / "server").mkdir()
    (root / "web").mkdir()
    (root / "mod" / "src" / "main" / "java" / "com" / "ex").mkdir(parents=True)
    (root / "src" / "shared" / "Util.luau").write_text(
        "--!strict\nlocal Util = {}\nfunction Util.clamp(x, a, b)\n  if x < a then return a elseif x > b then return b end\n"
        "  return x\nend\nlocal function unusedHelper()\n  return 1\nend\nreturn Util\n", encoding="utf-8")
    loop = "local total = 0\nfor i = 1, 10 do\n  total = total + i\n  total = total * 2\n  total = total - 1\n  print(total)\nend\n"
    (root / "src" / "server" / "Combat.server.luau").write_text(
        '--!strict\nlocal Util = require(game:GetService("ReplicatedStorage"):WaitForChild("Shared"):WaitForChild("Util"))\n'
        "local function damage(hp, amount)\n  return Util.clamp(hp - amount, 0, 100)\nend\n"
        "local function onTouched(hit)\n  damage(100, 10)\nend\nworkspace.Part.Touched:Connect(onTouched)\n" + loop,
        encoding="utf-8")
    (root / "src" / "server" / "Hud.server.luau").write_text("--!strict\n" + loop, encoding="utf-8")
    (root / "web" / "a.js").write_text("import { b } from './b.js';\nexport function a() { return b(); }\n", encoding="utf-8")
    (root / "web" / "b.js").write_text("import { a } from './a';\nexport function b() { return 1; }\nfunction spare() {}\n", encoding="utf-8")
    (root / "mod" / "src" / "main" / "java" / "com" / "ex" / "Gun.java").write_text(
        "package com.ex;\nimport com.ex.Ammo;\npublic class Gun {\n  public void fire() { Ammo.take(); }\n}\n", encoding="utf-8")
    (root / "mod" / "src" / "main" / "java" / "com" / "ex" / "Ammo.java").write_text(
        "package com.ex;\npublic class Ammo {\n  public static void take() {}\n  private static void leftover() {}\n}\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(root), "game")
    yield reg
    storage._DEFAULT_STORAGE = None


def _ok(reg, cid, args):
    res = reg.run(cid, args)
    assert res["status"] == "ok", res
    return res["data"]


def test_callers_name_the_enclosing_function(proj) -> None:
    data = _ok(proj, "code.callers", {"project_id": "game", "name": "clamp"})
    assert data["definitions"][0]["file"] == "src/shared/Util.luau"
    assert data["callers"] == [{"file": "src/server/Combat.server.luau", "line": 4, "in": "damage"}]


def test_module_graph_resolves_luau_js_java_and_finds_cycles(proj) -> None:
    g = _ok(proj, "code.graph", {"project_id": "game"})
    edges = {(e["from"], e["to"]) for e in g["edges"]}
    assert ("src/server/Combat.server.luau", "src/shared/Util.luau") in edges
    assert ("web/a.js", "web/b.js") in edges and ("web/b.js", "web/a.js") in edges
    assert ("mod/src/main/java/com/ex/Gun.java", "mod/src/main/java/com/ex/Ammo.java") in edges
    assert ["web/a.js", "web/b.js"] in g["two_way_dependencies"]
    assert g["mermaid"].startswith("graph LR")


def test_unused_skips_callbacks_and_exports(proj) -> None:
    names = {f["name"] for f in _ok(proj, "code.unused", {"project_id": "game"})["likely_unused"]}
    assert {"unusedHelper", "spare", "leftover"} <= names
    assert not names & {"onTouched", "damage", "clamp", "a", "b", "take"}


def test_duplicates_and_complexity(proj) -> None:
    dup = _ok(proj, "code.duplicates", {"project_id": "game", "min_lines": 4})
    files = {o["file"] for b in dup["blocks"] for o in b["occurrences"]}
    assert dup["duplicate_blocks"] == 1 and files == {"src/server/Combat.server.luau", "src/server/Hud.server.luau"}
    cx = _ok(proj, "code.complexity", {"project_id": "game", "top": 3})
    assert cx["hotspots"][0]["function"] == "Util.clamp" and cx["hotspots"][0]["complexity"] == 3


def test_api_and_overview_are_compact(proj) -> None:
    import json
    api = _ok(proj, "code.api", {"project_id": "game"})["api"]
    assert {s["name"] for s in api["src/shared/Util.luau"]} == {"Util.clamp"}
    ov = _ok(proj, "code.overview", {"project_id": "game"})
    assert ov["functions"] >= 8 and ov["duplicate_blocks"] == 1
    assert len(json.dumps(ov)) < 5000
