"""Phase 2.2 review tests: rules for Luau/JS/Java/Kotlin, no false hits in
comments or strings, diff preview, approve-to-apply, stale protection, undo."""
from __future__ import annotations

from pathlib import Path

import pytest

from cie.review import review_text


def _rules(text, lang, path="x"):
    return {(f["rule"], f["line"]) for f in review_text(text, lang, path)}


def test_luau_rules() -> None:
    src = ('local Players = game.Players\n-- wait(1) only a comment\nlocal s = "spawn(x)"\nwait(1)\n'
           'spawn(function() end)\ntask.wait(1)\nwhile true do\n  print("x")\nend\nwhile true do\n  task.wait()\nend\n'
           'r.OnServerEvent:Connect(function(player, amount)\n  player.Cash.Value += amount\nend)\n'
           'r.OnServerEvent:Connect(function(player, n)\n  if typeof(n) ~= "number" then return end\nend)\n')
    hits = _rules(src, "luau")
    assert ("luau.strict-mode", 1) in hits and ("luau.get-service", 1) in hits
    assert ("luau.deprecated-wait", 4) in hits and ("luau.deprecated-wait", 2) not in hits
    assert ("luau.deprecated-spawn", 5) in hits and ("luau.deprecated-spawn", 3) not in hits
    assert ("luau.loop-without-yield", 7) in hits and ("luau.loop-without-yield", 10) not in hits
    assert ("luau.remote-unvalidated", 13) in hits and ("luau.remote-unvalidated", 16) not in hits


def test_js_and_hearth_rules() -> None:
    src = ('var x = 1;\nif (a == b && c != null) {}\nel.innerHTML = userName;\nel.innerHTML = "<b>ok</b>";\n'
           'const s = JSON.parse(localStorage.getItem("save"));\nHearth.unlock("win");\n'
           'if (window.Hearth) Hearth.unlock("ok");\nconst t = "a == b"; // eval(x)\nindexedDB.open("db");\n')
    hits = _rules(src, "javascript", "games/g/game.js")
    assert {("js.var", 1), ("js.loose-equality", 2), ("js.inner-html", 3), ("js.unsafe-save-parse", 5),
            ("hearth.unguarded-unlock", 6), ("hearth.storage", 9)} <= hits
    assert ("js.inner-html", 4) not in hits and ("hearth.unguarded-unlock", 7) not in hits
    assert not any(line == 8 for _, line in hits)
    assert ("hearth.storage", 9) not in _rules(src, "javascript", "src/game.js")  # only for Hearth games


def test_java_kotlin_minecraft_rules() -> None:
    java = ('import net.minecraft.client.MinecraftClient;\npublic class Gun {\n  void f(String a) {\n'
            '    if (a == "x") {}\n    try { g(); } catch (Exception e) {}\n    System.out.println(a);\n  }\n}\n')
    hits = _rules(java, "java", "src/main/java/com/ex/Gun.java")
    assert {("mc.client-import-in-common", 1), ("java.string-equality", 4), ("java.empty-catch", 5), ("mc.print-debug", 6)} <= hits
    assert ("mc.client-import-in-common", 1) not in _rules(java, "java", "src/client/java/com/ex/GunRenderer.java")
    kt = 'class A {\n  fun f(x: String?) { println(x!!.length) }\n}\n'
    assert {("kotlin.not-null-assert", 2), ("kotlin.string-print", 2)} <= _rules(kt, "kotlin")


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
    root.mkdir()
    (root / "Main.server.luau").write_bytes(b"local Players = game.Players\r\nwait(1)\r\nprint('ok')\r\n")
    (root / "web.js").write_text("var a = 1;\nif (a == 2) {}\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(root), "game")
    yield reg, root
    storage._DEFAULT_STORAGE = None


def test_review_preview_apply_and_undo(proj) -> None:
    reg, root = proj
    rev = reg.run("code.review", {"project_id": "game"})["data"]
    assert rev["fixable"] == 5 and rev["counts"]["warning"] >= 2
    preview = reg.run("code.fix_preview", {"project_id": "game"})["data"]
    assert preview["fixes"] == 5
    assert '+local Players = game:GetService("Players")' in preview["diff"] and "+task.wait(1)" in preview["diff"]
    assert (root / "web.js").read_text() == "var a = 1;\nif (a == 2) {}\n"  # preview changed nothing
    ids = [i for i in preview["fix_ids"] if i.startswith("web.js")]
    assert reg.run("code.fix_apply", {"project_id": "game", "fix_ids": ids})["status"] == "approval_required"
    done = reg.run("code.fix_apply", {"project_id": "game", "fix_ids": ids}, approved=True)["data"]
    assert sorted(done["applied"]) == sorted(ids)
    assert (root / "web.js").read_text() == "let a = 1;\nif (a === 2) {}\n"
    lua_ids = [i for i in preview["fix_ids"] if i.startswith("Main")]
    reg.run("code.fix_apply", {"project_id": "game", "fix_ids": lua_ids}, approved=True)
    raw = (root / "Main.server.luau").read_bytes()
    assert raw.startswith(b"--!strict\r\nlocal Players = game:GetService(\"Players\")\r\ntask.wait(1)\r\n")  # CRLF kept
    reg.run("storage.restore", {"snapshot_id": done["snapshot_id"]}, approved=True)
    assert (root / "web.js").read_text() == "var a = 1;\nif (a == 2) {}\n"


def test_stale_fixes_are_skipped(proj) -> None:
    reg, root = proj
    ids = reg.run("code.fix_preview", {"project_id": "game", "rules": ["js.var"]})["data"]["fix_ids"]
    (root / "web.js").write_text("const a = 1;\nif (a == 2) {}\n", encoding="utf-8")  # user edited meanwhile
    res = reg.run("code.fix_apply", {"project_id": "game", "fix_ids": ids}, approved=True)
    assert res["status"] == "error" and "changed" in res["summary"]
    assert (root / "web.js").read_text() == "const a = 1;\nif (a == 2) {}\n"


def test_unknown_rule_is_rejected(proj) -> None:
    reg, _ = proj
    assert reg.run("code.review", {"project_id": "game", "rules": ["nope"]})["status"] == "error"
    assert len(reg.run("code.rules", {})["data"]["rules"]) >= 20
