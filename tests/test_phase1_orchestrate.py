"""Phase 1.1 orchestration tests: planning, parallel runs, references,
permission gating, retries, reuse, and dependency handling."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()  # Windows runners can hand back short/alias paths
    home = tmp_path / "home"
    monkeypatch.setenv("CIE_HOME", str(home))
    monkeypatch.setenv("CIE_VAULT", str(home / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    proj = tmp_path / "game"
    proj.mkdir()
    (proj / "player.lua").write_text("local function Jump() end\nfunction Player.update(dt) end\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(proj), "game")
    yield reg, proj
    storage._DEFAULT_STORAGE = None


def _ok(reg, cid, args):
    res = reg.run(cid, args)
    assert res["status"] == "ok", res
    return res["data"]


def test_plan_covers_each_part_of_the_task(env) -> None:
    reg, _ = env
    p = _ok(reg, "orchestrate.plan", {"task": "my sprites look blurry and the jump sound is too loud", "project_id": "game"})
    ids = [s["capability_id"] for s in p["steps"]]
    assert ids[0] == "knowledge.summary"
    assert any(i.startswith("art.") for i in ids) and any(i.startswith("audio.") for i in ids)
    assert sum(1 for s in p["steps"] if s.get("permission") not in (None, "read")) <= 2
    changes = [s for s in p["steps"] if s.get("permission") not in (None, "read")]
    assert all("approval" in s and s.get("after") for s in changes)


def test_plan_prefers_new_storage_and_marks_unavailable(env) -> None:
    reg, _ = env
    undo = [s["capability_id"] for s in _ok(reg, "orchestrate.plan", {"task": "undo the last change"})["steps"]]
    assert "storage.restore_preview" in undo and not any(i.startswith("workflow.snapshot") for i in undo)
    find = _ok(reg, "orchestrate.plan", {"task": "find where the player class is defined", "project_id": "game"})
    sem = [s for s in find["steps"] if s["capability_id"] == "search.semantic_query"]
    assert sem and sem[0]["alternative"] == "eff.search_files" and "unavailable" in sem[0]


def test_run_executes_reads_in_parallel_with_references(env) -> None:
    reg, _ = env
    res = _ok(reg, "orchestrate.run", {"steps": [
        {"id": "ctx", "capability_id": "knowledge.summary", "args": {"project_id": "game"}},
        {"id": "sym", "capability_id": "coding.symbol_search", "args": {"query": "Jump", "project_id": "game"}},
        {"id": "src", "capability_id": "coding.read_symbol", "after": ["sym"],
         "args": {"path": "$steps.sym.matches.0.file_path", "symbol_name": "$steps.sym.matches.0.name"}},
    ]})
    assert res["ok"] is True
    src = next(s for s in res["steps"] if s["id"] == "src")
    assert "Jump" in src["data"]["source"]


def test_run_never_approves_changes(env) -> None:
    reg, proj = env
    res = _ok(reg, "orchestrate.run", {"steps": [
        {"id": "w", "capability_id": "audio.procedural_sfx", "args": {"output_path": str(proj / "x.wav")}},
    ]})
    assert res["needs_approval"] == ["w"] and not (proj / "x.wav").exists()


def test_run_skips_dependents_after_failure_and_reuses_reads(env) -> None:
    reg, _ = env
    res = _ok(reg, "orchestrate.run", {"stop_on_error": False, "steps": [
        {"id": "bad", "capability_id": "knowledge.summary", "args": {"project_id": "nope"}},
        {"id": "dep", "capability_id": "knowledge.summary", "args": {"project_id": "game"}, "after": ["bad"]},
        {"id": "a", "capability_id": "project.list", "args": {}},
        {"id": "b", "capability_id": "project.list", "args": {}, "after": ["a"]},
    ]})
    by = {s["id"]: s for s in res["steps"]}
    assert by["bad"]["status"] == "error" and by["dep"]["status"] == "skipped"
    assert by["b"]["status"] == "ok" and by["b"].get("reused") is True


def test_run_retries_transient_errors(env) -> None:
    reg, _ = env
    from cie.registry import CapabilitySpec
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"status": "error", "summary": "database is locked"}
        return {"status": "ok", "summary": "done", "data": {"n": calls["n"]}}

    reg.register(CapabilitySpec(id="test.flaky", area="test", summary="flaky", input_schema={},
                                permission="read", handler=flaky))
    res = _ok(reg, "orchestrate.run", {"steps": [{"id": "f", "capability_id": "test.flaky", "args": {}}]})
    assert res["ok"] and res["steps"][0]["attempts"] == 3


def test_parallel_reads_actually_overlap(env) -> None:
    reg, _ = env
    from cie.registry import CapabilitySpec
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(args):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.15)
        with lock:
            active["now"] -= 1
        return {"status": "ok", "summary": "ok", "data": {"k": args.get("k")}}

    reg.register(CapabilitySpec(id="test.slow", area="test", summary="slow", input_schema={"k": "optional int"},
                                permission="read", handler=slow))
    _ok(reg, "orchestrate.run", {"steps": [{"capability_id": "test.slow", "args": {"k": i}} for i in range(4)]})
    assert active["max"] >= 2


def test_run_rejects_bad_steps(env) -> None:
    reg, _ = env
    assert reg.run("orchestrate.run", {"steps": [{"capability_id": "nope.nope"}]})["status"] == "error"
    assert reg.run("orchestrate.run", {"steps": [{"capability_id": "orchestrate.plan", "args": {"task": "x"}}]})["status"] == "error"
    assert reg.run("orchestrate.run", {"steps": [{"id": "a", "capability_id": "project.list", "after": ["zz"]}]})["status"] == "error"
