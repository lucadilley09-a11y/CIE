"""Phase 2.3-2.5 tests: suite detection, running tests, static checks,
disposable workspaces (diff, promote, discard), and the dev workflow."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "home" / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n", encoding="utf-8")
    (root / "main.luau").write_text("--!strict\nwhile true do\n  print('x')\nend\n", encoding="utf-8")
    (root / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
    (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
    (root / "spec").mkdir()
    (root / "spec" / "Combat.spec.luau").write_text("--!strict\nreturn function() end\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(root), "proj")
    yield reg, root
    storage._DEFAULT_STORAGE = None


def test_detect_lists_every_suite_and_its_needs(env) -> None:
    reg, _ = env
    suites = {s["suite"]: s for s in reg.run("test.detect", {"project_id": "proj"})["data"]["suites"]}
    assert {"pytest", "gradle", "testez", "static"} <= set(suites)
    assert suites["testez"]["available"] is False and "Roblox Studio" in suites["testez"]["needs"]
    assert suites["static"]["available"] is True


def test_run_pytest_and_static_checks(env) -> None:
    reg, _ = env
    assert reg.run("test.run", {"project_id": "proj", "suite": "pytest"})["status"] == "approval_required"
    res = reg.run("test.run", {"project_id": "proj", "suite": "pytest", "timeout_sec": 120}, approved=True)
    pyr = res["data"]["results"][0]
    assert pyr["ok"] is True and pyr["passed"] == 1, pyr
    static = reg.run("test.run", {"project_id": "proj", "suite": "static"}, approved=True)["data"]
    assert static["ok"] is False and static["results"][0]["errors"] == 1  # endless loop in main.luau


def test_unavailable_suite_is_reported_not_run(env) -> None:
    reg, _ = env
    res = reg.run("test.run", {"project_id": "proj", "suite": "testez"}, approved=True)["data"]
    assert res["results"][0]["skipped"] and "Roblox Studio" in res["results"][0]["reason"]


def test_workspace_create_diff_promote_discard(env) -> None:
    reg, root = env
    ws = reg.run("workspace.create", {"project_id": "proj"}, approved=True)["data"]
    wpath = Path(ws["path"])
    assert (wpath / "calc.py").exists() and ws["files_copied"] >= 5
    (wpath / "calc.py").write_text("def add(a, b):\n    return int(a) + int(b)\n", encoding="utf-8")
    (wpath / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert "int(a)" not in (root / "calc.py").read_text()  # original untouched
    diff = reg.run("workspace.diff", {"workspace_id": ws["workspace_id"]})["data"]
    assert diff["modified"] == ["calc.py"] and diff["added"] == ["new.py"]
    assert any("+    return int(a) + int(b)" in ln for d in diff["text_diffs"] for ln in d["diff"])
    prom = reg.run("workspace.promote", {"workspace_id": ws["workspace_id"], "paths": ["calc.py"]}, approved=True)["data"]
    assert prom["promoted"] == ["calc.py"] and "int(a)" in (root / "calc.py").read_text()
    assert not (root / "new.py").exists()
    reg.run("storage.restore", {"snapshot_id": prom["snapshot_id"]}, approved=True)
    assert "int(a)" not in (root / "calc.py").read_text()
    reg.run("workspace.discard", {"workspace_id": ws["workspace_id"]}, approved=True)
    assert not wpath.exists() and (root / "calc.py").exists()
    assert reg.run("workspace.list", {})["data"]["count"] == 0


def test_workspace_of_a_workspace_is_refused(env) -> None:
    reg, _ = env
    ws = reg.run("workspace.create", {"project_id": "proj"}, approved=True)["data"]
    again = reg.run("workspace.create", {"project_id": ws["workspace_id"]}, approved=True)
    assert again["status"] == "error"


def test_dev_workflow_has_ten_steps_and_a_model_pick(env) -> None:
    reg, _ = env
    wf = reg.run("dev.workflow", {"task": "redesign the combat networking architecture", "project_id": "proj"})["data"]
    assert len(wf["steps"]) == 10 and "workspace" in wf["work_in"]
    assert wf["steps"][3]["model"]["model"] == "opus"
