"""Phase 2.3-2.5: unified testing, disposable workspaces, and the
inspect -> plan -> change -> test -> fix -> verify development workflow.

Test commands come from a fixed table here (never from caller input) and run
without a shell, with a timeout, in the project folder. Every project also
gets CIE's own static checks, so Luau code with no runnable test suite still
gets verified.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .config import MODEL_WEIGHT_EXTS, get_cie_home, iter_project_files, load_config, project_roots, redact_secrets, save_config
from .efficiency import errors_only

# --------------------------------------------------------------------------
# 2.3 Unified testing
# --------------------------------------------------------------------------


def _root(project_id: str) -> dict[str, Any]:
    roots = [r for r in project_roots() if r["id"] == project_id]
    if not roots:
        raise ValueError(f"No registered project with id '{project_id}'.")
    return roots[0]


def detect_suites(project_id: str) -> dict[str, Any]:
    root = _root(project_id)
    base = Path(root["path"])
    suites: list[dict[str, Any]] = []
    py_tests = [p for p in iter_project_files(root, extensions={".py"}) if p.name.startswith("test_") or p.name.endswith("_test.py")]
    if py_tests:
        suites.append({"suite": "pytest", "files": len(py_tests), "needs": "python with pytest installed",
                       "available": shutil.which("python") is not None or shutil.which("py") is not None})
    pkg = base / "package.json"
    if pkg.is_file():
        import json

        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {})
        except (ValueError, OSError):
            scripts = {}
        if "test" in scripts:
            suites.append({"suite": "npm", "script": scripts["test"][:120], "needs": "Node.js (npm)", "available": shutil.which("npm") is not None})
    js_tests = [p for p in iter_project_files(root, extensions={".js", ".mjs"}) if re.search(r"\.test\.m?js$", p.name)]
    if js_tests and not any(s["suite"] == "npm" for s in suites):
        suites.append({"suite": "node", "files": len(js_tests), "needs": "Node.js", "available": shutil.which("node") is not None})
    gradlew = base / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if gradlew.is_file() and any((base / f).is_file() for f in ("build.gradle", "build.gradle.kts")):
        suites.append({"suite": "gradle", "needs": "Java (JDK) for the Gradle wrapper", "available": shutil.which("java") is not None})
    spec = [p for p in iter_project_files(root, extensions={".luau", ".lua"}) if re.search(r"\.spec\.luau?$", p.name)]
    if spec:
        suites.append({"suite": "testez", "files": len(spec), "available": False,
                       "needs": "Roblox Studio (TestEZ specs run inside Studio; Phase 4 integration)"})
    suites.append({"suite": "static", "available": True, "needs": "nothing (CIE built-in)",
                   "covers": "code.review errors and warnings for Luau, JS, Java, Kotlin"})
    return {"project_id": project_id, "suites": suites}


def _command(suite: str, base: Path) -> list[str]:
    if suite == "pytest":
        exe = shutil.which("python") or shutil.which("py") or "python"
        return [exe, "-m", "pytest", "-q", "--no-header"]
    if suite == "npm":
        return [shutil.which("npm") or "npm", "test", "--silent"]
    if suite == "node":
        return [shutil.which("node") or "node", "--test"]
    if suite == "gradle":
        return [str(base / ("gradlew.bat" if os.name == "nt" else "gradlew")), "test", "--console=plain"]
    raise ValueError(f"Suite '{suite}' cannot be run from CIE.")


def _parse(suite: str, out: str, code: int) -> dict[str, Any]:
    nums = lambda pat: int(m.group(1)) if (m := re.search(pat, out)) else None  # noqa: E731
    if suite == "pytest":
        return {"passed": nums(r"(\d+) passed"), "failed": nums(r"(\d+) failed"), "skipped": nums(r"(\d+) skipped"),
                "errors": nums(r"(\d+) errors?")}
    if suite == "node":
        return {"passed": nums(r"#\s*pass\s+(\d+)"), "failed": nums(r"#\s*fail\s+(\d+)")}
    if suite == "gradle":
        return {"build": "succeeded" if "BUILD SUCCESSFUL" in out else "failed" if "BUILD FAILED" in out else "unknown",
                "tests": nums(r"(\d+) tests? completed"), "failed": nums(r"(\d+) failed")}
    return {}


def run_tests(project_id: str, suite: str = "", timeout_sec: int = 300) -> dict[str, Any]:
    from .review import review

    root = _root(project_id)
    base = Path(root["path"])
    detected = {s["suite"]: s for s in detect_suites(project_id)["suites"]}
    to_run = [suite] if suite else [s for s, info in detected.items() if info["available"] and s != "testez"]
    results: list[dict[str, Any]] = []
    for name in to_run:
        if name == "static":
            rev = review(project_id, min_severity="warning", limit=20)
            results.append({"suite": "static", "ok": rev["counts"]["error"] == 0, "errors": rev["counts"]["error"],
                            "warnings": rev["counts"]["warning"], "top": rev["findings"][:10]})
            continue
        if name not in detected:
            results.append({"suite": name, "ok": False, "skipped": True, "reason": "not found in this project"})
            continue
        if not detected[name]["available"]:
            results.append({"suite": name, "ok": False, "skipped": True, "reason": f"needs {detected[name]['needs']}"})
            continue
        cmd = _command(name, base)
        try:
            proc = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, shell=False,
                                  timeout=min(900, max(10, int(timeout_sec))))
            out = redact_secrets((proc.stdout or "") + "\n" + (proc.stderr or ""))
            results.append({"suite": name, "ok": proc.returncode == 0, "exit_code": proc.returncode,
                            **{k: v for k, v in _parse(name, out, proc.returncode).items() if v is not None},
                            "problems": errors_only(out, max_items=15).get("items", []),
                            "tail": out.strip().splitlines()[-12:]})
        except subprocess.TimeoutExpired:
            results.append({"suite": name, "ok": False, "timed_out": True, "timeout_sec": timeout_sec})
        except OSError as exc:
            results.append({"suite": name, "ok": False, "reason": f"could not start: {exc}"})
    ran = [r for r in results if not r.get("skipped")]
    return {"project_id": project_id, "ok": bool(ran) and all(r["ok"] for r in ran), "results": results}


# --------------------------------------------------------------------------
# 2.4 Disposable workspaces
# --------------------------------------------------------------------------

def _ws_root() -> Path:
    p = get_cie_home() / "workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree(root_entry: dict[str, Any]) -> dict[str, Path]:
    base = Path(root_entry["path"])
    return {p.relative_to(base).as_posix(): p for p in iter_project_files(root_entry, include_binary=True)
            if p.suffix.lower() not in MODEL_WEIGHT_EXTS}


def workspace_create(project_id: str, label: str = "") -> dict[str, Any]:
    from .projects import register_project

    origin = _root(project_id)
    if origin.get("workspace_of"):
        raise ValueError("That project is already a workspace; create one from the original project.")
    wid = f"{project_id}--ws-{uuid.uuid4().hex[:6]}"
    dest = _ws_root() / wid
    files = _tree(origin)
    for rel, src in files.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    dest.mkdir(parents=True, exist_ok=True)
    register_project(str(dest), wid, list(origin.get("excludes") or []), auto_excludes=False)
    cfg = load_config()
    for entry in cfg.get("projects") or []:
        if isinstance(entry, dict) and entry.get("id") == wid:
            entry["workspace_of"] = project_id
            entry["label"] = label
    save_config(cfg)
    return {"workspace_id": wid, "path": str(dest), "files_copied": len(files), "origin": project_id,
            "note": "Edit and test freely here. workspace.diff shows changes; workspace.promote copies chosen files back."}


def _ws(workspace_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ws = _root(workspace_id)
    if not ws.get("workspace_of"):
        raise ValueError(f"'{workspace_id}' is not a CIE workspace.")
    return ws, _root(ws["workspace_of"])


def workspace_diff(workspace_id: str, max_diff_lines: int = 120) -> dict[str, Any]:
    ws, origin = _ws(workspace_id)
    a, b = _tree(origin), _tree(ws)
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    modified = sorted(r for r in set(a) & set(b) if a[r].stat().st_size != b[r].stat().st_size or _sha(a[r]) != _sha(b[r]))
    diffs, budget = [], max(0, int(max_diff_lines))
    for rel in modified:
        if budget <= 0:
            break
        try:
            old = a[rel].read_text(encoding="utf-8").splitlines()
            new = b[rel].read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        lines = list(difflib.unified_diff(old, new, f"original/{rel}", f"workspace/{rel}", lineterm="", n=2))[:budget]
        budget -= len(lines)
        diffs.append({"file": rel, "diff": lines})
    return {"workspace_id": workspace_id, "origin": origin["id"], "added": added, "removed": removed,
            "modified": modified, "text_diffs": diffs}


def workspace_promote(workspace_id: str, paths: list[str] | None = None, include_deletions: bool = False) -> dict[str, Any]:
    from .snapstore import create_snapshot

    ws, origin = _ws(workspace_id)
    d = workspace_diff(workspace_id, 0)
    changed = d["added"] + d["modified"]
    chosen = [p for p in changed if not paths or p in set(paths)]
    deletions = [p for p in d["removed"] if include_deletions and (not paths or p in set(paths))]
    if not chosen and not deletions:
        raise ValueError("Nothing to promote (no matching changes).")
    obase, wbase = Path(origin["path"]), Path(ws["path"])
    snap = create_snapshot(paths=[str(obase / p) for p in chosen + deletions], kind="auto",
                           description=f"Before promoting workspace {workspace_id}")
    for rel in chosen:
        target = obase / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wbase / rel, target)
    for rel in deletions:
        (obase / rel).unlink(missing_ok=True)
    return {"promoted": chosen, "deleted": deletions, "origin": origin["id"], "snapshot_id": snap["snapshot_id"],
            "undo": f"storage.restore with snapshot_id {snap['snapshot_id']}"}


def workspace_discard(workspace_id: str) -> dict[str, Any]:
    from .projects import remove_project

    ws, _ = _ws(workspace_id)
    path = Path(ws["path"]).resolve()
    if _ws_root().resolve() not in path.parents:
        raise ValueError("Refusing to delete a folder outside CIE's workspaces directory.")
    remove_project(workspace_id)
    shutil.rmtree(path, ignore_errors=True)
    return {"discarded": workspace_id, "original_untouched": True}


def workspace_list() -> dict[str, Any]:
    out = [{"workspace_id": r["id"], "origin": r["workspace_of"], "label": r.get("label", ""), "path": r["path"]}
           for r in project_roots() if r.get("workspace_of")]
    return {"workspaces": out, "count": len(out)}


# --------------------------------------------------------------------------
# 2.5 Development workflow
# --------------------------------------------------------------------------

def dev_workflow(task: str, project_id: str, risky: bool = False) -> dict[str, Any]:
    from .routing import pick_model

    _root(project_id)
    model = pick_model(task)
    work_in = "a workspace (workspace.create), then workspace.promote" if risky or model["complexity"] == "high" else "the project, with automatic snapshots"
    steps = [
        {"step": 1, "do": "Inspect the project", "use": "knowledge.summary", "args": {"project_id": project_id}},
        {"step": 2, "do": "Understand the code involved", "use": "code.overview / code.callers / code.graph", "args": {"project_id": project_id}},
        {"step": 3, "do": "Find the problem", "use": "code.review, coding.stack_trace, eff.search_files", "args": {"project_id": project_id}},
        {"step": 4, "do": "Plan the change", "use": "routing.recommend", "args": {"task": task},
         "model": {k: model[k] for k in ("model", "effort", "delegate_to_helper")}},
        {"step": 5, "do": f"Make the change in {work_in}", "use": "storage.snapshot before direct edits; code.fix_apply for review fixes"},
        {"step": 6, "do": "Run the tests", "use": "test.run", "args": {"project_id": project_id}},
        {"step": 7, "do": "Read failures", "use": "eff.errors_only, coding.stack_trace"},
        {"step": 8, "do": "Repair and repeat 6-7 until green", "use": "code.fix_preview / code.fix_apply"},
        {"step": 9, "do": "Verify", "use": "test.run (all suites) and code.review min_severity=error", "args": {"project_id": project_id}},
        {"step": 10, "do": "Summarize and record", "use": "knowledge.record (decision or issue fixed)"},
    ]
    return {"task": task, "project_id": project_id, "work_in": work_in, "steps": steps,
            "undo": "Every change is snapshotted; storage.restore_preview then storage.restore rolls back."}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return {"status": "ok", "summary": summary, "data": fn(args)}
            except (ValueError, PermissionError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    reg.register(CapabilitySpec(
        id="test.detect", area="test", summary="Find the test suites a project can run (pytest, npm/node, Gradle, TestEZ, CIE static checks) and what each needs.",
        input_schema={"project_id": "str"}, permission="read",
        handler=wrap(lambda a: detect_suites(a["project_id"]), "Suites detected."), tags=["test", "verify"]))
    reg.register(CapabilitySpec(
        id="test.run", area="test",
        summary="Run a project's tests (one suite or all available) and CIE's static checks, with pass/fail counts and the key error lines.",
        input_schema={"project_id": "str", "suite": "optional str", "timeout_sec": "optional int"}, permission="exec",
        handler=wrap(lambda a: run_tests(a["project_id"], a.get("suite", ""), int(a.get("timeout_sec", 300))), "Tests finished."),
        tags=["test", "verify", "pytest", "node", "gradle"]))
    reg.register(CapabilitySpec(
        id="workspace.create", area="workspace",
        summary="Copy a project into a disposable workspace for risky changes. The original is not touched.",
        input_schema={"project_id": "str", "label": "optional str"}, permission="write",
        handler=wrap(lambda a: workspace_create(a["project_id"], a.get("label", "")), "Workspace created."),
        tags=["workspace", "sandbox", "safe"]))
    reg.register(CapabilitySpec(
        id="workspace.diff", area="workspace", summary="Show what changed in a workspace compared with its original project.",
        input_schema={"workspace_id": "str", "max_diff_lines": "optional int"}, permission="read",
        handler=wrap(lambda a: workspace_diff(a["workspace_id"], int(a.get("max_diff_lines", 120))), "Workspace diff ready."),
        tags=["workspace", "diff"]))
    reg.register(CapabilitySpec(
        id="workspace.promote", area="workspace",
        summary="Copy chosen (or all) changed files from a workspace back to the original project, with a snapshot first.",
        input_schema={"workspace_id": "str", "paths": "optional list[str]", "include_deletions": "optional bool"}, permission="write",
        handler=wrap(lambda a: workspace_promote(a["workspace_id"], a.get("paths"), a.get("include_deletions", False)), "Workspace promoted."),
        tags=["workspace", "apply"]))
    reg.register(CapabilitySpec(
        id="workspace.discard", area="workspace", summary="Delete a workspace copy. The original project is never touched.",
        input_schema={"workspace_id": "str"}, permission="write",
        handler=wrap(lambda a: workspace_discard(a["workspace_id"]), "Workspace discarded."), tags=["workspace"]))
    reg.register(CapabilitySpec(
        id="workspace.list", area="workspace", summary="List disposable workspaces and their original projects.",
        input_schema={}, permission="read", handler=wrap(lambda a: workspace_list(), "Workspaces listed."), tags=["workspace"]))
    reg.register(CapabilitySpec(
        id="dev.workflow", area="dev",
        summary="Step-by-step plan to fix a bug or add a feature: inspect, understand, find, plan (model pick), change safely, test, repair, verify, record.",
        input_schema={"task": "str", "project_id": "str", "risky": "optional bool"}, permission="read",
        handler=wrap(lambda a: dev_workflow(a["task"], a["project_id"], a.get("risky", False)), "Workflow ready."),
        tags=["dev", "workflow", "bug", "feature", "plan"]))
