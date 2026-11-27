"""Reusable workflows (Phase 4.1 and 4.3).

A workflow is JSON: named inputs and a list of steps. Each step calls one CIE capability.

    {"name": "...", "description": "...",
     "inputs": {"source": {"required": true}, "bevel": {"default": 0.02}},
     "steps": [
       {"id": "polish", "capability": "blender.process",
        "args": {"source": "$inputs.source", "ops": [{"op": "bevel", "width": "$inputs.bevel"}]}},
       {"id": "place", "capability": "studio.place", "when": "$inputs.dest",
        "args": {"source": "$steps.polish.data.files.0", "dest": "$inputs.dest"}}]}

Values: "$inputs.x", "$item" / "$item.x" (inside foreach), "$steps.<id>.data.<path>", and
"{inputs.x}"-style text templates inside longer strings.
Step options:
  when      run only if a reference is truthy, or "<ref> == <json>" / "!=" / "in" / "not in"
  foreach   run once per element of a list (reads run 4 at a time); the step's data becomes
            {"items": [...], "files": [all files], "primary": [first file of each item]}
  retry     extra attempts after an error (default 1)
  require   "pass" or "warn": the result's verdict must be at least this good, otherwise retry
  vary      on a quality retry, add 1 to this numeric arg (e.g. "params.seed" or "seed")
  optional  a failure doesn't stop the run

Approval (owner decision: once per run). If any step can change files (write/exec), flow.run
only prepares the run and lists every change it can make. The owner approves that exact plan
outside Claude (`cie approve <run_id>` in a terminal, or the status page); Claude can't approve.
flow.resume then runs it. Changing the workflow or its inputs invalidates the approval.

Failure (owner decision: retry, then roll back). If a step still fails after its retries, every
file change the run made is undone from the automatic snapshots, including files it created.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import get_cie_home

REF_RE = re.compile(r"^\$(inputs|item|steps)(?:\.(.+))?$")
TPL_RE = re.compile(r"\{(inputs|item|steps)(?:\.([^{}]+))?\}")
RANK = {"pass": 2, "warn": 1, "fail": 0}


def _root(sub: str) -> Path:
    d = get_cie_home() / "flows" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Built-in workflows
# ---------------------------------------------------------------------------

BUILTINS: dict[str, dict[str, Any]] = {
    "blender_polish": {
        "name": "blender_polish",
        "description": "Take a CIE (or any) model through Blender: real bevels, optional smoothing and material, bake the detail into "
                       "normal/AO/colour maps on the original game mesh, render it, and optionally place it in a project.",
        "inputs": {"source": {"required": True, "description": "model file (studio or project)"},
                   "bevel": {"default": 0.015, "description": "bevel width in metres"},
                   "subdivide": {"default": 0, "description": "Catmull-Clark levels for the detailed version (0 = none)"},
                   "material": {"default": "", "description": "optional procedural material preset"},
                   "bake": {"default": True}, "texture_size": {"default": 1024},
                   "formats": {"default": ["glb"]}, "dest": {"default": "", "description": "project folder to place the result in"}},
        "steps": [
            {"id": "detail", "capability": "blender.process", "retry": 1,
             "args": {"source": "$inputs.source", "render": False,
                      "ops": [{"op": "bevel", "width": "$inputs.bevel", "segments": 3},
                              {"op": "subdivide", "levels": "$inputs.subdivide", "skip_if_zero": True},
                              {"op": "shade_smooth", "angle": 35},
                              {"op": "material", "preset": "$inputs.material", "skip_if_empty": True}]}},
            {"id": "bake", "capability": "blender.bake", "when": "$inputs.bake", "retry": 1,
             "args": {"source": "$inputs.source", "high": "$steps.detail.data.files.0", "size": "$inputs.texture_size",
                      "formats": "$inputs.formats", "views": "sheet"}},
            {"id": "render", "capability": "blender.render", "when": "$inputs.bake == false",
             "args": {"source": "$steps.detail.data.files.0", "views": "sheet"}},
            {"id": "place", "capability": "studio.place", "when": "$inputs.dest",
             "args": {"source": "$steps.bake.data.files.0", "dest": "$inputs.dest"}},
        ],
    },
    "asset_pack": {
        "name": "asset_pack",
        "description": "Make a set of assets (any studio tools), check each one, remake failures with a new seed, then optionally place "
                       "them all in a project folder.",
        "inputs": {"items": {"required": True, "description": "[{capability: 'mesh.generate', args: {...}}, ...]"},
                   "dest": {"default": "", "description": "project folder for the finished assets"},
                   "require": {"default": "pass"}},
        "steps": [
            {"id": "make", "capability": "$item.capability", "foreach": "$inputs.items", "args": "$item.args",
             "require": "$inputs.require", "retry": 2, "vary": "params.seed"},
            {"id": "place", "capability": "studio.place", "when": "$inputs.dest", "foreach": "$steps.make.data.primary",
             "args": {"source": "$item", "dest": "$inputs.dest"}},
        ],
    },
    "bug_hunt": {
        "name": "bug_hunt",
        "description": "Find likely bugs in a project: code review rules, unused code, complexity hot spots, then run its tests. "
                       "Claude reads the results, fixes, and runs it again to confirm.",
        "inputs": {"project_id": {"required": True}, "suite": {"default": ""}},
        "steps": [
            {"id": "review", "capability": "code.review", "args": {"project_id": "$inputs.project_id"}},
            {"id": "unused", "capability": "code.unused", "args": {"project_id": "$inputs.project_id"}, "optional": True},
            {"id": "complexity", "capability": "code.complexity", "args": {"project_id": "$inputs.project_id"}, "optional": True},
            {"id": "suites", "capability": "test.detect", "args": {"project_id": "$inputs.project_id"}},
            {"id": "tests", "capability": "test.run", "args": {"project_id": "$inputs.project_id", "suite": "$inputs.suite"}},
        ],
    },
    "release_prep": {
        "name": "release_prep",
        "description": "Get a browser game ready to ship: code review, tests, Hearth/asset checks, zip it, optionally copy the zip into the project.",
        "inputs": {"project_id": {"required": True}, "game_dir": {"required": True, "description": "the game's folder"},
                   "version": {"default": ""}, "dest": {"default": "", "description": "project folder for the zip (e.g. dist)"}},
        "steps": [
            {"id": "review", "capability": "code.review", "args": {"project_id": "$inputs.project_id"}},
            {"id": "tests", "capability": "test.run", "args": {"project_id": "$inputs.project_id"}, "optional": True},
            {"id": "check", "capability": "game.check", "args": {"path": "$inputs.game_dir"}, "require": "warn"},
            {"id": "zip", "capability": "game.package", "args": {"path": "$inputs.game_dir", "version": "$inputs.version"}},
            {"id": "place", "capability": "studio.place", "when": "$inputs.dest",
             "args": {"source": "$steps.zip.data.zip", "dest": "$inputs.dest"}},
        ],
    },
    "new_browser_game": {
        "name": "new_browser_game",
        "description": "Start a Hearth-ready browser game in one run: create the game from the Hearth template, make its assets, place them "
                       "in the game's assets folder, check the Hearth integration, and zip a first build. Claude then writes the gameplay.",
        "inputs": {"project_root": {"required": True, "description": "a registered folder (e.g. your Hearth repo) - games/<game_id>/ is created in it"},
                   "game_id": {"required": True}, "title": {"required": True},
                   "assets": {"default": [], "description": "[{capability, args}] - same as asset_pack items"}},
        "steps": [
            {"id": "create", "capability": "templates.apply",
             "args": {"id": "hearth-browser-game", "target_dir": "$inputs.project_root",
                      "params": {"game_id": "$inputs.game_id", "title": "$inputs.title"}}},
            {"id": "make", "capability": "$item.capability", "foreach": "$inputs.assets", "args": "$item.args",
             "require": "pass", "retry": 2, "vary": "params.seed"},
            {"id": "place", "capability": "studio.place", "foreach": "$steps.make.data.primary",
             "args": {"source": "$item", "dest": "{inputs.project_root}/games/{inputs.game_id}/assets"}},
            {"id": "check", "capability": "game.check", "args": {"path": "{inputs.project_root}/games/{inputs.game_id}"}, "require": "warn"},
            {"id": "zip", "capability": "game.package", "args": {"path": "{inputs.project_root}/games/{inputs.game_id}", "version": "0.1.0"}},
        ],
    },
}


# ---------------------------------------------------------------------------
# References and conditions
# ---------------------------------------------------------------------------


def _walk(cur: Any, path: str | None, label: str) -> Any:
    if not path:
        return cur
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(f"'{label}' does not exist.")
    return cur


def _lookup(kind: str, path: str | None, ctx: dict[str, Any]) -> Any:
    label = f"${kind}" + (f".{path}" if path else "")
    if kind == "inputs":
        return _walk(ctx["inputs"], path, label)
    if kind == "item":
        if "item" not in ctx:
            raise KeyError(f"'{label}' used outside a foreach step.")
        return _walk(ctx["item"], path, label)
    sid, _, rest = (path or "").partition(".")
    if sid not in ctx["steps"]:
        raise KeyError(f"'{label}': step '{sid}' hasn't run (or was skipped).")
    return _walk(ctx["steps"][sid], rest or None, label)


def resolve(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        m = REF_RE.match(value)
        if m:
            return _lookup(m.group(1), m.group(2), ctx)
        if TPL_RE.search(value):
            return TPL_RE.sub(lambda mm: str(_lookup(mm.group(1), mm.group(2), ctx)), value)
        return value
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


def condition(expr: Any, ctx: dict[str, Any]) -> bool:
    if expr is None:
        return True
    if isinstance(expr, bool):
        return expr
    text = str(expr).strip()
    for op in (" not in ", " == ", " != ", " in "):
        if op in text:
            left, right = text.split(op, 1)
            lv = resolve(left.strip(), ctx)
            try:
                rv = json.loads(right.strip())
            except ValueError:
                rv = resolve(right.strip(), ctx)
            return {" == ": lambda: lv == rv, " != ": lambda: lv != rv, " in ": lambda: lv in rv, " not in ": lambda: lv not in rv}[op]()
    try:
        return bool(resolve(text, ctx))
    except KeyError:
        return False


def _clean_ops(args: Any) -> Any:
    """Drop ops a workflow marked optional when their value is empty/zero (e.g. no material chosen)."""
    if isinstance(args, dict) and isinstance(args.get("ops"), list):
        kept = []
        for op in args["ops"]:
            if op.get("skip_if_zero") and not op.get("levels"):
                continue
            if op.get("skip_if_empty") and not op.get("preset"):
                continue
            kept.append({k: v for k, v in op.items() if k not in ("skip_if_zero", "skip_if_empty")})
        args = {**args, "ops": kept}
    return args


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


def get(name_or_def: Any) -> dict[str, Any]:
    if isinstance(name_or_def, dict):
        return copy.deepcopy(name_or_def)
    name = str(name_or_def)
    if name in BUILTINS:
        return copy.deepcopy(BUILTINS[name])
    p = _root("library") / f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    raise ValueError(f"No workflow '{name}'. Workflows: {', '.join(n['name'] for n in list_all())}.")


def list_all() -> list[dict[str, Any]]:
    out = [{"name": k, "origin": "built-in", "description": v["description"], "inputs": v.get("inputs", {})} for k, v in BUILTINS.items()]
    for p in sorted(_root("library").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"name": d["name"], "origin": "saved", "description": d.get("description", ""), "inputs": d.get("inputs", {})})
        except (ValueError, KeyError, OSError):
            continue
    return out


def validate(reg: Any, wf: dict[str, Any]) -> list[str]:
    problems = []
    if not wf.get("name"):
        problems.append("Workflow needs a name.")
    steps = wf.get("steps") or []
    if not steps:
        problems.append("Workflow needs at least one step.")
    ids = set()
    for i, s in enumerate(steps):
        sid = s.get("id") or f"s{i + 1}"
        if sid in ids:
            problems.append(f"Duplicate step id '{sid}'.")
        ids.add(sid)
        cap = s.get("capability", "")
        if not cap:
            problems.append(f"Step {sid} has no capability.")
        elif not cap.startswith("$") and not reg.get(cap):
            problems.append(f"Step {sid}: unknown capability '{cap}'.")
        elif cap.startswith(("flow.", "orchestrate.", "workflow.")):
            problems.append(f"Step {sid}: workflows can't start other workflows.")
        refs = re.findall(r"\$steps\.([A-Za-z0-9_-]+)|\{steps\.([A-Za-z0-9_-]+)", json.dumps(s))
        for a, b in refs:
            if (a or b) not in ids - {sid}:
                problems.append(f"Step {sid} refers to step '{a or b}', which doesn't come before it.")
        if s.get("require") and s["require"] not in RANK and not str(s["require"]).startswith("$"):
            problems.append(f"Step {sid}: require must be pass or warn.")
    return problems


def save(reg: Any, wf: dict[str, Any]) -> dict[str, Any]:
    problems = validate(reg, wf)
    if problems:
        raise ValueError("Workflow not saved: " + " ".join(problems))
    if wf["name"] in BUILTINS:
        raise ValueError(f"'{wf['name']}' is a built-in workflow; save yours under another name.")
    p = _root("library") / f"{re.sub(r'[^A-Za-z0-9_-]', '_', wf['name'])}.json"
    p.write_text(json.dumps(wf, indent=1), encoding="utf-8")
    return {"saved": str(p), "name": wf["name"]}


# ---------------------------------------------------------------------------
# Planning, approval, running
# ---------------------------------------------------------------------------


def _inputs(wf: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    spec = wf.get("inputs") or {}
    out = {}
    for k, v in spec.items():
        if k in given:
            out[k] = given[k]
        elif v.get("required"):
            raise ValueError(f"Workflow '{wf['name']}' needs input '{k}'.")
        else:
            out[k] = copy.deepcopy(v.get("default"))
    extra = set(given) - set(spec)
    if extra:
        raise ValueError(f"Unknown input(s): {', '.join(sorted(extra))}. Inputs: {', '.join(spec)}.")
    return out


def plan_hash(wf: dict[str, Any], inputs: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"wf": wf, "inputs": inputs}, sort_keys=True, default=str).encode()).hexdigest()


def plan(reg: Any, wf: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """Every change the run could make, resolved as far as possible before it starts."""
    changes, reads = [], []
    ctx = {"inputs": inputs, "steps": {}}
    for i, s in enumerate(wf["steps"]):
        sid = s.get("id") or f"s{i + 1}"
        cond = s.get("when")
        try:
            will = condition(cond, ctx) if cond and "$steps" not in str(cond) else True
        except KeyError:
            will = True
        if not will:
            continue
        items: list[Any] = [None]
        if s.get("foreach"):
            try:
                items = list(resolve(s["foreach"], ctx))
            except KeyError:
                items = ["(decided during the run)"]
        for item in items:
            c = {**ctx, "item": item} if s.get("foreach") else ctx
            try:
                cap = resolve(s.get("capability"), c)
            except KeyError:
                cap = s.get("capability")
            spec = reg.get(cap) if isinstance(cap, str) else None
            perm = spec.permission if spec else "unknown"
            try:
                args = resolve(s.get("args") or {}, c)
            except KeyError:
                args = "(depends on earlier steps)"
            entry = {"step": sid, "capability": cap, "permission": perm, "args": args}
            (changes if perm in ("write", "exec", "unknown") else reads).append(entry)
    return {"changes": changes, "reads": len(reads)}


def _save_run(run: dict[str, Any]) -> None:
    (_root("runs") / f"{run['run_id']}.json").write_text(json.dumps(run, indent=1, default=str), encoding="utf-8")


def load_run(run_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id or ""):
        raise ValueError("Bad run id.")
    p = _root("runs") / f"{run_id}.json"
    if not p.exists():
        raise ValueError(f"No workflow run '{run_id}'.")
    return json.loads(p.read_text(encoding="utf-8"))


def approve(run_id: str) -> dict[str, Any]:
    """Owner-side approval. Called from the CLI / status page, never exposed to Claude."""
    run = load_run(run_id)
    if run["status"] != "awaiting_approval":
        raise ValueError(f"Run {run_id} is '{run['status']}', not waiting for approval.")
    (_root("approvals") / f"{run_id}.json").write_text(json.dumps({"hash": run["hash"], "approved_at": time.strftime("%Y-%m-%d %H:%M:%S")}),
                                                       encoding="utf-8")
    run["status"] = "approved"
    _save_run(run)
    return run


def _approved(run: dict[str, Any]) -> bool:
    p = _root("approvals") / f"{run['run_id']}.json"
    if not p.exists():
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("hash") == plan_hash(run["workflow"], run["inputs"])
    except (ValueError, OSError):
        return False


def start(reg: Any, workflow: Any, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    wf = get(workflow)
    problems = validate(reg, wf)
    if problems:
        raise ValueError("Workflow is not valid: " + " ".join(problems))
    ins = _inputs(wf, inputs or {})
    pl = plan(reg, wf, ins)
    run = {"run_id": time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6], "name": wf["name"], "workflow": wf, "inputs": ins,
           "hash": plan_hash(wf, ins), "plan": pl, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": {}, "log": []}
    if pl["changes"]:
        run["status"] = "awaiting_approval"
        _save_run(run)
        return {"run_id": run["run_id"], "status": "awaiting_approval", "changes": pl["changes"], "reads": pl["reads"],
                "how_to_approve": f"In a terminal: cie approve {run['run_id']}  (shows the plan, asks y/N). Then call flow.resume.",
                "note": "Claude can't approve runs. Changing the workflow or inputs needs a new approval."}
    run["status"] = "running"
    _save_run(run)
    return execute(reg, run, approved=False)


def resume(reg: Any, run_id: str) -> dict[str, Any]:
    run = load_run(run_id)
    if run["status"] in ("done", "failed", "rolled_back", "cancelled"):
        raise ValueError(f"Run {run_id} already finished ({run['status']}).")
    if run["plan"]["changes"] and not _approved(run):
        raise PermissionError(f"Run {run_id} isn't approved yet. The owner approves it with: cie approve {run_id}")
    run["status"] = "running"
    _save_run(run)
    return execute(reg, run, approved=bool(run["plan"]["changes"]))


def _call(reg: Any, cap: str, args: dict[str, Any], allowed: set[str], approved: bool) -> dict[str, Any]:
    spec = reg.get(cap)
    if spec is None:
        return {"status": "error", "summary": f"Unknown capability '{cap}'.", "data": {}}
    if cap.startswith(("flow.", "orchestrate.", "workflow.")):
        return {"status": "error", "summary": "Workflows can't start other workflows.", "data": {}}
    needs = spec.permission in ("write", "exec")
    if needs and cap not in allowed:
        return {"status": "error", "summary": f"'{cap}' changes files but wasn't in the approved plan.", "data": {}}
    return reg.run(cap, args, approved=approved and needs)


def _run_step(reg: Any, s: dict[str, Any], ctx: dict[str, Any], allowed: set[str], approved: bool, log: list[str]) -> dict[str, Any]:
    cap = resolve(s["capability"], ctx)
    args = _clean_ops(resolve(s.get("args") or {}, ctx))
    if not isinstance(args, dict):
        return {"status": "error", "summary": "Step args must be an object.", "data": {}}
    retries = max(0, int(resolve(s.get("retry", 1), ctx)))
    need = resolve(s.get("require"), ctx) if s.get("require") else None
    env: dict[str, Any] = {}
    for attempt in range(retries + 1):
        env = _call(reg, cap, args, allowed, approved)
        env["attempts"] = attempt + 1
        if env["status"] == "ok" and need:
            v = (env.get("data") or {}).get("verdict", "pass")
            if RANK.get(v, 0) < RANK.get(need, 2):
                log.append(f"{s['id']}: verdict '{v}' below '{need}' (attempt {attempt + 1})")
                env = {**env, "status": "quality_fail", "summary": f"Result verdict '{v}', needs '{need}'."}
                if s.get("vary"):
                    args = _bump(args, s["vary"])
                continue
        if env["status"] == "ok":
            return env
        if env["status"] in ("approval_required", "tool_missing"):
            return env
        log.append(f"{s['id']}: {env['status']} - {str(env.get('summary'))[:160]} (attempt {attempt + 1})")
    return env


def _bump(args: dict[str, Any], path: str) -> dict[str, Any]:
    args = copy.deepcopy(args)
    cur = args
    parts = path.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = int(cur.get(parts[-1], 0) or 0) + 1
    return args


def _snapshots(env: dict[str, Any]) -> list[str]:
    """Snapshot ids a step took before changing files (registry auto-snapshots, or ones a capability
    took itself and reported, like templates.apply)."""
    ids = [e["auto_snapshot"] for e in env.get("evidence") or [] if isinstance(e, dict) and e.get("auto_snapshot")]
    data = env.get("data") if isinstance(env.get("data"), dict) else {}
    for key in ("snapshot_id", "snapshot"):
        if isinstance(data.get(key), str) and data[key] not in ids:
            ids.append(data[key])
    return ids


def execute(reg: Any, run: dict[str, Any], approved: bool) -> dict[str, Any]:
    wf, ins = run["workflow"], run["inputs"]
    allowed = {c["capability"] for c in run["plan"]["changes"] if isinstance(c["capability"], str)}
    ctx: dict[str, Any] = {"inputs": ins, "steps": {}}
    snaps: list[str] = []
    report: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    failed = None
    for i, s in enumerate(wf["steps"]):
        s = {**s, "id": s.get("id") or f"s{i + 1}"}
        try:
            if not condition(s.get("when"), ctx):
                report.append({"id": s["id"], "status": "skipped", "summary": "Condition not met."})
                continue
            if s.get("foreach"):
                items = resolve(s["foreach"], ctx)
                if not isinstance(items, list):
                    raise ValueError(f"foreach for step {s['id']} isn't a list.")
                reads = all((reg.get(resolve(s["capability"], {**ctx, "item": it})) or type("x", (), {"permission": "write"})).permission == "read" for it in items)

                def one(it: Any, s: dict[str, Any] = s) -> dict[str, Any]:
                    return _run_step(reg, s, {**ctx, "item": it}, allowed, approved, run["log"])

                if reads and len(items) > 1:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        envs = list(pool.map(one, items))
                else:
                    envs = [one(it) for it in items]
                for e in envs:
                    snaps += _snapshots(e)
                datas = [e.get("data") or {} for e in envs]
                files = [f for d in datas for f in (d.get("files") or [])]
                primary = [(d.get("files") or [d.get("zip")])[0] for d in datas if (d.get("files") or d.get("zip"))]
                bad = [e for e in envs if e["status"] != "ok"]
                env = {"status": "ok" if not bad else bad[0]["status"], "summary": f"{len(envs) - len(bad)}/{len(envs)} items ok."
                       + (f" First problem: {bad[0].get('summary')}" if bad else ""),
                       "data": {"items": datas, "files": files, "primary": primary}}
            else:
                env = _run_step(reg, s, ctx, allowed, approved, run["log"])
                snaps += _snapshots(env)
        except (KeyError, ValueError) as exc:
            env = {"status": "error", "summary": str(exc), "data": {}}
        ctx["steps"][s["id"]] = {"status": env["status"], "data": env.get("data") or {}}
        report.append({"id": s["id"], "status": env["status"], "summary": str(env.get("summary", ""))[:300],
                       "attempts": env.get("attempts", 1), "verdict": (env.get("data") or {}).get("verdict") if isinstance(env.get("data"), dict) else None})
        if env["status"] != "ok" and not s.get("optional"):
            failed = s["id"]
            break
    rolled: list[str] = []
    if failed and snaps:
        from .snapstore import restore_snapshot
        for sid in reversed(snaps):
            try:
                # path snapshots delete files the run created; project-wide snapshots don't remove
                # 'added' files, so nothing the owner made meanwhile is ever deleted
                restore_snapshot(sid, remove_added=False)
                rolled.append(sid)
            except Exception as exc:  # noqa: BLE001 - keep undoing the rest, report what failed
                run["log"].append(f"rollback of {sid} failed: {exc}")
    run["status"] = "done" if not failed else ("rolled_back" if rolled else "failed")
    run["steps"] = {r["id"]: r for r in report}
    run["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    run["snapshots"] = snaps
    _save_run(run)
    outputs = {sid: _summary(v["data"]) for sid, v in ctx["steps"].items()}
    out = {"run_id": run["run_id"], "status": run["status"], "steps": report, "outputs": outputs,
           "elapsed_s": round(time.perf_counter() - t0, 2), "log": run["log"][-20:]}
    if failed:
        out["failed_step"] = failed
        out["rolled_back_snapshots"] = rolled
        if rolled:
            out["note"] = "The run failed, so every project change it made was undone. Studio outputs are kept for inspection."
    return out


def _summary(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    keep = {k: data[k] for k in ("files", "primary", "preview", "verdict", "zip", "issues", "copied") if k in data}
    if "items" in data:
        keep["items"] = [{k: d.get(k) for k in ("files", "preview", "verdict") if k in d} for d in data["items"]]
    if isinstance(keep.get("files"), list) and len(keep["files"]) > 20:
        keep["files"] = keep["files"][:20] + [f"... {len(data['files']) - 20} more"]
    if isinstance(keep.get("issues"), list):
        keep["issues"] = keep["issues"][:8]
    return keep or {k: v for k, v in list(data.items())[:8]}


def history(limit: int = 20, status: str = "") -> list[dict[str, Any]]:
    runs = []
    for p in sorted(_root("runs").glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if status and r.get("status") != status:
            continue
        runs.append({"run_id": r["run_id"], "name": r["name"], "status": r["status"], "created": r.get("created"),
                     "finished": r.get("finished"), "changes": len(r["plan"]["changes"])})
        if len(runs) >= int(limit):
            break
    return runs


def cancel(run_id: str) -> dict[str, Any]:
    run = load_run(run_id)
    if run["status"] in ("done", "failed", "rolled_back"):
        raise ValueError("That run already finished.")
    run["status"] = "cancelled"
    _save_run(run)
    return {"run_id": run_id, "status": "cancelled"}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                st = "approval_required" if isinstance(data, dict) and data.get("status") == "awaiting_approval" else "ok"
                text = summary if st == "ok" else "Run prepared; waiting for the owner to approve its changes."
                if isinstance(data, dict) and data.get("status") in ("failed", "rolled_back"):
                    text = f"Workflow {data['status'].replace('_', ' ')} at step {data.get('failed_step')}."
                return {"status": st, "summary": text, "data": data}
            except (ValueError, KeyError, PermissionError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    reg.register(CapabilitySpec(
        id="flow.list", area="workflow", summary="List workflows (built-in: blender_polish, asset_pack, bug_hunt, release_prep, new_browser_game; plus saved ones) and their inputs.",
        input_schema={}, permission="read", handler=wrap(lambda a: {"workflows": list_all()}, "Workflows listed."), tags=["workflow", "automation"]))
    reg.register(CapabilitySpec(
        id="flow.show", area="workflow", summary="Show a workflow's full definition.",
        input_schema={"name": "str"}, permission="read", handler=wrap(lambda a: get(a["name"]), "Workflow shown."), tags=["workflow"]))
    reg.register(CapabilitySpec(
        id="flow.validate", area="workflow", summary="Check a workflow definition (capabilities exist, step references point backwards).",
        input_schema={"workflow": "object"}, permission="read",
        handler=wrap(lambda a: {"problems": validate(reg, a["workflow"])}, "Workflow checked."), tags=["workflow"]))
    reg.register(CapabilitySpec(
        id="flow.save", area="workflow", summary="Save a workflow definition to CIE's workflow library so it can be run by name later. Saving changes no project files.",
        input_schema={"workflow": "object"}, permission="read", handler=wrap(lambda a: save(reg, a["workflow"]), "Workflow saved."), tags=["workflow"]))
    reg.register(CapabilitySpec(
        id="flow.run", area="workflow",
        summary="Run a workflow by name or definition with inputs. Read-only runs go straight through. Runs that change files are prepared "
                "and listed for the owner to approve once (cie approve <run_id>); then call flow.resume. Failed steps retry, then the run rolls back.",
        input_schema={"workflow": "str|object", "inputs": "optional object"}, permission="read",
        handler=wrap(lambda a: start(reg, a["workflow"], a.get("inputs") or {}), "Workflow finished."), tags=["workflow", "automation", "pipeline"]))
    reg.register(CapabilitySpec(
        id="flow.resume", area="workflow", summary="Run a workflow the owner has approved (only the exact approved plan can run).",
        input_schema={"run_id": "str"}, permission="read", handler=wrap(lambda a: resume(reg, a["run_id"]), "Workflow finished."), tags=["workflow"]))
    reg.register(CapabilitySpec(
        id="flow.history", area="workflow", summary="List recent workflow runs and their status (awaiting_approval, done, failed, rolled_back).",
        input_schema={"limit": "optional int", "status": "optional str"}, permission="read",
        handler=wrap(lambda a: {"runs": history(int(a.get("limit", 20)), a.get("status", ""))}, "Runs listed."), tags=["workflow", "history"]))
    reg.register(CapabilitySpec(
        id="flow.get", area="workflow", summary="Show one workflow run: plan, step results, log.",
        input_schema={"run_id": "str"}, permission="read",
        handler=wrap(lambda a: {k: v for k, v in load_run(a["run_id"]).items() if k != "workflow"}, "Run shown."), tags=["workflow"]))
    reg.register(CapabilitySpec(
        id="flow.cancel", area="workflow", summary="Cancel a run that hasn't finished (nothing is undone; it just won't run).",
        input_schema={"run_id": "str"}, permission="read", handler=wrap(lambda a: cancel(a["run_id"]), "Run cancelled."), tags=["workflow"]))
