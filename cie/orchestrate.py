"""Phase 1.1 capability orchestration.

orchestrate.plan turns a plain-language task into an ordered, dependency-aware
list of CIE capability steps: context first, read-only checks (runnable in
parallel), then changes (approval-gated). It marks steps whose external tool
is missing and suggests a built-in alternative where one exists.

orchestrate.run executes such a list: independent read-only steps run in
parallel, write/exec steps run one at a time through the normal permission
gate (so this runner can never approve anything itself), transient failures
are retried, identical calls are reused, and later steps can reference
earlier results with "$steps.<id>.<field>".

MCP does not guarantee Claude will call a tool on its own; this module makes
the right calls easy to find and cheap to make.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# External tool each capability needs. Capabilities not listed run on CIE's
# own Python code (Tier 1) or have a built-in fallback.
REQUIRES_TOOL = {
    "lint.ruff": "ruff", "lint.mypy": "mypy", "lint.pyright": "pyright",
    "lint.gdlint": "gdlint", "lint.gdformat": "gdformat",
    "security.bandit": "bandit", "security.gitleaks": "gitleaks", "security.semgrep": "semgrep",
    "deps.audit": "pip-audit", "deps.licenses": "pip-licenses",
    "coding.coverage": "coverage", "coding.profile": "py-spy",
    "sandbox.run": "docker", "engine.godot_check": "godot",
    "audio.ffmpeg_convert": "ffmpeg", "audio.sox_effect": "sox", "audio.midi_render": "fluidsynth",
    "audio.puredata_run": "pd", "audio.supercollider_run": "sclang",
    "art.aseprite_export": "aseprite",
    "art.blender_export": "blender", "art.blender_render": "blender", "art.blender_report": "blender",
}
# Older capabilities kept for compatibility; the planner suggests the newer ones.
SUPERSEDED = {"workflow.snapshot_create": "storage.snapshot", "workflow.snapshot_restore": "storage.restore"}
NEEDS_LOCAL_MODEL = {"search.semantic_query": "bge-small-en-v1.5", "search.semantic_index": "bge-small-en-v1.5"}
MAX_CHANGE_STEPS = 2

ALTERNATIVES = {
    "search.semantic_query": "eff.search_files",
    "search.semantic_index": "eff.incremental_index",
    "security.bandit": "coding.security_checks",
    "security.semgrep": "coding.security_checks",
    "art.blender_report": "art.inspect_3d",
    "lint.ruff": "coding.security_checks",
}

# Plain-language words → capability areas/ids that handle them.
SYNONYMS: dict[str, list[str]] = {
    "sprite": ["art", "sprite", "atlas", "png", "2d"], "sprites": ["art", "sprite", "atlas", "2d"],
    "texture": ["art", "tile", "png", "palette"], "textures": ["art", "tile", "png"],
    "image": ["art", "image", "png"], "images": ["art", "image", "png"], "png": ["png", "art"],
    "palette": ["palette", "art"], "color": ["palette"], "colours": ["palette"], "colors": ["palette"],
    "tileset": ["tile", "atlas", "art"], "atlas": ["atlas"],
    "sound": ["audio"], "sounds": ["audio"], "sfx": ["audio", "sfx"], "music": ["audio", "midi"],
    "loud": ["lufs", "audio"], "loudness": ["lufs"], "clipping": ["clipping", "audio"], "wav": ["audio"],
    "model": ["mesh", "3d", "gltf"], "models": ["mesh", "3d", "gltf"], "mesh": ["mesh"],
    "3d": ["3d", "mesh", "gltf"], "glb": ["gltf"], "gltf": ["gltf"], "obj": ["mesh", "3d"],
    "polygons": ["mesh", "simplify"], "lod": ["simplify", "mesh"],
    "bug": ["coding", "stack", "errors"], "error": ["errors", "stack", "coding"], "errors": ["errors", "stack"],
    "crash": ["stack", "errors"], "traceback": ["stack"], "log": ["errors", "dedupe", "log"],
    "test": ["tests", "coding"], "tests": ["tests"], "coverage": ["coverage"],
    "security": ["security"], "secrets": ["security", "gitleaks"], "vulnerability": ["deps", "audit"],
    "dependencies": ["dependencies", "deps"], "imports": ["dependencies"],
    "function": ["symbol"], "class": ["symbol"], "where": ["search", "symbol"], "find": ["search", "symbol"],
    "search": ["search"], "overview": ["knowledge", "summary"], "understand": ["knowledge", "summary", "brain"],
    "project": ["knowledge", "project"], "architecture": ["knowledge", "brain", "dependencies"],
    "backup": ["snapshot"], "undo": ["snapshot", "restore"], "rollback": ["restore", "snapshot"],
    "revert": ["restore"], "snapshot": ["snapshot"], "disk": ["storage", "report"], "space": ["storage", "report", "duplicates"],
    "duplicate": ["duplicates"], "duplicates": ["duplicates"], "compress": ["compression", "png"],
    "optimize": ["optimize", "compression", "simplify"], "assets": ["assets", "knowledge"],
    "issue": ["issue", "knowledge"], "issues": ["issue", "knowledge"], "decision": ["decision", "knowledge"],
    "remember": ["knowledge", "memory"], "note": ["memory", "knowledge"], "notes": ["memory"],
    "godot": ["godot", "gdscript"], "gdscript": ["gdlint", "gdscript"], "roblox": ["lua", "luau"],
    "lint": ["lint"], "type": ["mypy", "pyright"], "git": ["git"], "commit": ["git"], "pr": ["github"],
}
STOPWORDS = {"the", "a", "an", "my", "and", "or", "to", "of", "in", "on", "for", "with", "is", "it", "this",
             "that", "all", "me", "i", "can", "you", "please", "check", "make", "do", "what", "how", "are",
             "game", "games", "why", "does", "my", "using", "much"}
TRANSIENT_MARKERS = ("database is locked", "timed out", "temporarily", "resource busy", "try again")
REF_RE = re.compile(r"^\$steps\.([A-Za-z0-9_-]+)\.(.+)$")


def _tokens(text: str) -> list[str]:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]
    out = list(words)
    for w in words:
        out.extend(SYNONYMS.get(w, []))
    return out


def _tool_available(cid: str) -> tuple[bool, str | None]:
    if cid in NEEDS_LOCAL_MODEL:
        from .models import list_models
        wanted = NEEDS_LOCAL_MODEL[cid]
        ok = any(m["id"] == wanted and m["installed"] for m in list_models()["models"])
        return ok, f"local model {wanted}"
    tool = REQUIRES_TOOL.get(cid)
    if not tool:
        return True, None
    if tool == "blender":
        from .config import load_config
        return bool(load_config().get("blender_path") or shutil.which("blender")), tool
    return shutil.which(tool) is not None, tool


def plan(reg: Any, task: str, project_id: str = "", max_steps: int = 6) -> dict[str, Any]:
    if not task.strip():
        raise ValueError("Describe the task in plain words.")
    tokens = _tokens(task)
    scored: list[tuple[float, dict[str, Any]]] = []
    for cap in reg.discover():
        cid = cap["id"]
        if cid.startswith(("orchestrate.", "core.batch")) or cid in SUPERSEDED:
            continue
        spec = reg.get(cid)
        tags = [t.lower() for t in (spec.tags if spec else [])]
        id_parts = re.findall(r"[a-z0-9]+", cid.lower())
        summary = cap["summary"].lower()
        score = 0.0
        for t in tokens:
            if t == cap["area"].lower():
                score += 3
            if t in id_parts:
                score += 3
            if t in tags:
                score += 2
            if len(t) > 2 and re.search(rf"\b{re.escape(t)}", summary):
                score += 1
        if score > 0:
            # Prefer read-only steps when tied; they are safe to run at once.
            score += 0.5 if cap["permission"] == "read" else 0
            scored.append((score, cap))
    scored.sort(key=lambda sc: (-sc[0], sc[1]["id"]))
    # Round-robin across areas so a task touching art AND audio gets both,
    # and cap changes so a plan stays mostly safe, read-only checks.
    limit = max(1, min(int(max_steps), 15))
    top = scored[0][0] if scored else 0
    # Keep only clearly relevant matches: weak word overlaps are noise.
    scored = [(sc, cap) for sc, cap in scored if sc >= 0.5 * top]
    by_area: dict[str, list[dict[str, Any]]] = {}
    for sc, cap in scored:
        by_area.setdefault(cap["area"], []).append(cap)
    by_area = {a: caps for a, caps in by_area.items()
               if max(sc for sc, c in scored if c["area"] == a) >= 0.6 * top}
    area_order = sorted(by_area, key=lambda a: -max(sc for sc, c in scored if c["area"] == a))
    picked: list[dict[str, Any]] = []
    changes_taken = 0
    while len(picked) < limit and any(by_area.values()):
        for area in area_order:
            queue = by_area[area]
            while queue:
                cap = queue.pop(0)
                if cap["permission"] != "read":
                    if changes_taken >= MAX_CHANGE_STEPS:
                        continue
                    changes_taken += 1
                picked.append(cap)
                break
            if len(picked) >= limit:
                break

    steps: list[dict[str, Any]] = []
    if project_id and not any(c["id"] == "knowledge.summary" for c in picked):
        steps.append({"id": "context", "capability_id": "knowledge.summary", "args": {"project_id": project_id},
                      "why": "Project overview first, so later steps start from known context.", "group": 1})
    reads = [c for c in picked if c["permission"] == "read"]
    changes = [c for c in picked if c["permission"] != "read"]
    for i, cap in enumerate(reads + changes, start=1):
        schema = cap["input_schema"] or {}
        args: dict[str, Any] = {}
        if project_id:
            if "project_id" in schema:
                args["project_id"] = project_id
            elif "project" in schema:
                args["project"] = project_id
        needs = [k for k, v in schema.items() if not str(v).startswith(("optional", "legacy")) and k not in args]
        available, tool = _tool_available(cap["id"])
        step: dict[str, Any] = {
            "id": f"s{i}", "capability_id": cap["id"], "args": args,
            "permission": cap["permission"], "summary": cap["summary"],
            "group": 1 if cap["permission"] == "read" else 2,
        }
        if needs:
            step["needs_args"] = needs
        if cap["permission"] != "read":
            step["approval"] = "Changes files; needs owner approval under 'assist'. A snapshot is taken automatically first."
            earlier_reads = [s["id"] for s in steps if s.get("group") == 1]
            if earlier_reads:
                step["after"] = earlier_reads
        if not available:
            step["unavailable"] = f"needs '{tool}', which is not installed"
            alt = ALTERNATIVES.get(cap["id"])
            if alt:
                step["alternative"] = alt
        steps.append(step)
    ready = [s for s in steps if "needs_args" not in s and "unavailable" not in s and s.get("group") == 1]
    return {
        "task": task,
        "project_id": project_id or None,
        "steps": steps,
        "runnable_now": [s["id"] for s in ready],
        "how_to_run": "Fill any needs_args, drop unwanted steps, then call orchestrate.run with the steps list. "
                      "Steps in group 1 run in parallel; group 2 (changes) run in order after them.",
        "matched_on": sorted(set(tokens))[:20],
    }


def _resolve_refs(value: Any, results: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, str):
        m = REF_RE.match(value)
        if not m:
            return value
        sid, path = m.group(1), m.group(2)
        if sid not in results:
            raise ValueError(f"Reference to unknown or unfinished step '{sid}'.")
        cur: Any = results[sid].get("data")
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                raise ValueError(f"'{value}' does not exist in step {sid}'s result.")
        return cur
    if isinstance(value, list):
        return [_resolve_refs(v, results) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_refs(v, results) for k, v in value.items()}
    return value


def _compact(env: dict[str, Any], budget: int) -> dict[str, Any]:
    out = {k: env.get(k) for k in ("status", "summary", "elapsed_ms", "approval_required")}
    data = env.get("data")
    text = json.dumps(data, default=str)
    if len(text) <= budget:
        out["data"] = data
    else:
        out["data_truncated"] = True
        out["data_keys"] = list(data.keys()) if isinstance(data, dict) else None
        out["data_preview"] = text[:min(budget, 600)]
    snaps = [e for e in env.get("evidence") or [] if isinstance(e, dict) and "auto_snapshot" in e]
    if snaps:
        out["auto_snapshot"] = snaps[0]["auto_snapshot"]
    return out


def run(reg: Any, steps: list[dict[str, Any]], stop_on_error: bool = True, max_parallel: int = 4,
        data_budget: int = 3000) -> dict[str, Any]:
    if not steps:
        raise ValueError("Provide at least one step.")
    ids = []
    for i, s in enumerate(steps):
        sid = str(s.get("id") or f"s{i + 1}")
        if sid in ids:
            raise ValueError(f"Duplicate step id '{sid}'.")
        ids.append(sid)
        s["id"] = sid
        if not reg.get(str(s.get("capability_id", ""))):
            raise ValueError(f"Step {sid}: unknown capability '{s.get('capability_id')}'.")
        if s.get("capability_id", "").startswith("orchestrate."):
            raise ValueError("Steps cannot call orchestrate.* (no nesting).")
    by_id = {s["id"]: s for s in steps}
    for s in steps:
        for dep in s.get("after") or []:
            if dep not in by_id:
                raise ValueError(f"Step {s['id']} waits on unknown step '{dep}'.")

    results: dict[str, dict[str, Any]] = {}
    report: dict[str, dict[str, Any]] = {}
    cache: dict[str, dict[str, Any]] = {}
    pending = list(ids)
    halted = False
    t0 = time.perf_counter()

    def execute(s: dict[str, Any]) -> dict[str, Any]:
        args = _resolve_refs(s.get("args") or {}, results)
        key = s["capability_id"] + json.dumps(args, sort_keys=True, default=str)
        spec = reg.get(s["capability_id"])
        if spec.permission == "read" and key in cache:
            return {**cache[key], "reused": True}
        attempts = 0
        while True:
            attempts += 1
            env = reg.run(s["capability_id"], args, approved=False)
            transient = env["status"] == "error" and any(m in str(env.get("summary", "")).lower() for m in TRANSIENT_MARKERS)
            if not transient or attempts >= 3:
                break
            time.sleep(0.2 * attempts)
        env["attempts"] = attempts
        if spec.permission == "read" and env["status"] == "ok":
            cache[key] = env
        return env

    while pending and not halted:
        ready = [sid for sid in pending
                 if all(d in results and results[d]["status"] == "ok" for d in by_id[sid].get("after") or [])]
        blocked = [sid for sid in pending
                   if any(d in results and results[d]["status"] != "ok" for d in by_id[sid].get("after") or [])]
        for sid in blocked:
            report[sid] = {"status": "skipped", "summary": "A step it depends on did not succeed."}
            results[sid] = {"status": "skipped", "data": {}}
            pending.remove(sid)
        if not ready:
            if pending and not blocked:
                for sid in pending:
                    report[sid] = {"status": "skipped", "summary": "Waiting on a step that never finished."}
                break
            continue
        reads = [sid for sid in ready if reg.get(by_id[sid]["capability_id"]).permission == "read"]
        batch = reads if reads else ready[:1]  # changes run one at a time
        if len(batch) > 1:
            with ThreadPoolExecutor(max_workers=max(1, min(int(max_parallel), 8))) as pool:
                outs = list(pool.map(lambda sid: (sid, execute(by_id[sid])), batch))
        else:
            outs = [(batch[0], execute(by_id[batch[0]]))]
        for sid, env in outs:
            pending.remove(sid)
            results[sid] = env
            report[sid] = {"capability_id": by_id[sid]["capability_id"], **_compact(env, data_budget),
                           "attempts": env.get("attempts", 1), **({"reused": True} if env.get("reused") else {})}
            if env["status"] not in {"ok", "approval_required"} and stop_on_error:
                halted = True
    if halted:
        for sid in pending:
            report[sid] = {"status": "skipped", "summary": "Stopped after an earlier error (stop_on_error)."}
    statuses = [r["status"] for r in report.values()]
    return {
        "ok": all(st == "ok" for st in statuses),
        "counts": {st: statuses.count(st) for st in sorted(set(statuses))},
        "needs_approval": [sid for sid, r in report.items() if r["status"] == "approval_required"],
        "steps": [{"id": sid, **report[sid]} for sid in ids if sid in report],
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def _plan(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"status": "ok", "summary": "Plan ready.",
                    "data": plan(reg, args["task"], args.get("project_id", ""), int(args.get("max_steps", 6)))}
        except ValueError as exc:
            return {"status": "error", "summary": str(exc)}

    def _run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = run(reg, args["steps"], args.get("stop_on_error", True), int(args.get("max_parallel", 4)))
        except ValueError as exc:
            return {"status": "error", "summary": str(exc)}
        summary = "All steps succeeded." if data["ok"] else f"Finished with {data['counts']}."
        return {"status": "ok", "summary": summary, "data": data}

    reg.register(CapabilitySpec(
        id="orchestrate.plan",
        area="orchestrate",
        summary="Turn a plain-language task into an ordered list of CIE steps (context, parallel checks, gated changes). Use this first for multi-step work.",
        input_schema={"task": "str", "project_id": "optional str", "max_steps": "optional int 1-15"},
        permission="read",
        handler=_plan,
        tags=["orchestrate", "plan", "workflow", "start"],
    ))
    # Every step goes through the normal permission gate with approved=False,
    # so this runner can never perform an unapproved change.
    reg.register(CapabilitySpec(
        id="orchestrate.run",
        area="orchestrate",
        summary="Run a list of CIE steps: parallel read-only checks, ordered changes through the permission gate, retries, result reuse, $steps.<id>.<field> references.",
        input_schema={"steps": "list[dict]", "stop_on_error": "optional bool", "max_parallel": "optional int 1-8"},
        permission="read",
        handler=_run,
        tags=["orchestrate", "run", "workflow", "parallel"],
    ))
