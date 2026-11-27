"""Phase 2.1 code intelligence built on the unified index.

Everything here reads the index (refreshed incrementally first), so answers
are fast and small instead of re-reading files. Results that depend on name
matching rather than real type resolution are labelled "inferred".
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .codescan import LANG_FAMILY, normalized_lines, windows
from .config import project_roots
from .efficiency import detect_language, incremental_index
from .storage import get_storage, sha256_text

# Names that run without an explicit call in the project: engine callbacks,
# entry points, and lifecycle hooks. They are never reported as unused.
ENTRY_POINTS = {
    "main", "init", "start", "setup", "constructor", "onInitialize", "onInitializeClient", "onInitializeServer",
    "onEnable", "onDisable", "onLoad", "toString", "equals", "hashCode", "compareTo", "run", "call", "apply",
    "update", "draw", "render", "tick", "onTick", "_ready", "_process", "_physics_process", "_init",
    "connectedCallback", "disconnectedCallback", "handleEvent", "__init__", "__call__", "__repr__", "__str__",
    "__enter__", "__exit__", "__eq__", "__hash__", "__len__", "__iter__", "__next__", "__getitem__",
}


def _pid_or_error(project_id: str) -> dict[str, Any]:
    roots = [r for r in project_roots() if r["id"] == project_id]
    if not roots:
        raise ValueError(f"No registered project with id '{project_id}'.")
    incremental_index(project_id)
    return roots[0]


def _rel(root: dict[str, Any], path: str) -> str:
    try:
        return Path(path).relative_to(root["path"]).as_posix()
    except ValueError:
        return path


def callers(project_id: str, name: str, limit: int = 50) -> dict[str, Any]:
    root = _pid_or_error(project_id)
    s = get_storage()
    defs = s.query("SELECT path, qualifier, kind, line_start, line_end FROM code_defs WHERE project_id = ? AND name = ? ORDER BY path, line_start",
                   (project_id, name))
    calls = s.query("SELECT path, line FROM code_calls WHERE project_id = ? AND name = ? ORDER BY path, line", (project_id, name))
    # Name which function each call sits inside, so the answer reads "X calls name".
    enclosing = []
    for c in calls[:limit]:
        inside = s.query(
            "SELECT name FROM code_defs WHERE path = ? AND line_start <= ? AND line_end >= ? AND kind != 'class' "
            "ORDER BY line_start DESC LIMIT 1", (c["path"], c["line"], c["line"]))
        enclosing.append({"file": _rel(root, c["path"]), "line": c["line"], "in": inside[0]["name"] if inside else "(top level)"})
    return {
        "name": name,
        "definitions": [{"file": _rel(root, d["path"]), "line": d["line_start"], "kind": d["kind"],
                         **({"qualifier": d["qualifier"]} if d["qualifier"] else {})} for d in defs],
        "call_count": len(calls),
        "callers": enclosing,
        "truncated": len(calls) > limit,
        "confidence": "inferred (matched by name)",
    }


# --------------------------------------------------------------------------
# Module graph
# --------------------------------------------------------------------------

def _stem(path: str) -> str:
    name = Path(path).name
    for suffix in (".server.luau", ".client.luau", ".server.lua", ".client.lua", ".luau", ".lua",
                   ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".py"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name in {"init", "index", "__init__"}:
        name = Path(path).parent.name
    return name


def _resolve(target: str, source: str, files: list[str], by_stem: dict[str, list[str]], family: str) -> str | None:
    if family == "js" and target.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(source), target))
        for cand in (base, *(base + e for e in (".js", ".mjs", ".ts", ".jsx", ".tsx")),
                     os.path.join(base, "index.js"), os.path.join(base, "index.ts")):
            for f in files:
                if os.path.normcase(f) == os.path.normcase(cand):
                    return f
        return None
    if family == "lua":
        m = re.findall(r"WaitForChild\(\s*\"([^\"]+)\"|FindFirstChild\(\s*\"([^\"]+)\"|\.([A-Za-z_]\w*)\s*$|^([A-Za-z_]\w*)$", target.strip())
        names = [next(g for g in groups if g) for groups in m if any(groups)]
        last = names[-1] if names else re.split(r"[.:\"'()\s]+", target.strip().strip("\"'"))[-1]
        cands = by_stem.get(last, [])
        return cands[0] if len(cands) == 1 else (sorted(cands, key=lambda c: len(os.path.commonpath([c, source])), reverse=True)[0] if cands else None)
    if family in {"java", "kotlin", "python"}:
        parts = target.split(".")
        last = parts[-1]
        if last == "*" or not last:
            return None
        cands = by_stem.get(last, [])
        if not cands and family == "python" and len(parts) > 1:
            cands = by_stem.get(parts[-2], [])
        if len(cands) > 1:
            pkg = "/".join(parts[:-1])
            better = [c for c in cands if pkg and pkg in c.replace("\\", "/")]
            cands = better or cands
        return cands[0] if cands else None
    return None


def module_graph(project_id: str, focus: str = "", max_edges: int = 200) -> dict[str, Any]:
    root = _pid_or_error(project_id)
    s = get_storage()
    files = [r["path"] for r in s.query("SELECT path FROM files_index WHERE project_id = ?", (project_id,))]
    by_stem: dict[str, list[str]] = defaultdict(list)
    for f in files:
        if LANG_FAMILY.get(detect_language(Path(f))):
            by_stem[_stem(f)].append(f)
    deps = s.query("SELECT source_file, target, kind FROM dependencies_index WHERE project_id = ?", (project_id,))
    edges: set[tuple[str, str]] = set()
    external: dict[str, int] = defaultdict(int)
    for d in deps:
        family = LANG_FAMILY.get(detect_language(Path(d["source_file"])), "")
        hit = _resolve(d["target"], d["source_file"], files, by_stem, family)
        if hit and hit != d["source_file"]:
            edges.add((_rel(root, d["source_file"]), _rel(root, hit)))
        elif not hit:
            external[d["target"].split(".")[0].split("/")[0]] += 1
    if focus:
        edges = {e for e in edges if focus in e[0] or focus in e[1]}
    incoming: dict[str, int] = defaultdict(int)
    outgoing: dict[str, int] = defaultdict(int)
    for a, b in edges:
        outgoing[a] += 1
        incoming[b] += 1
    ordered = sorted(edges)
    # Cycles between two modules are a common source of Roblox require errors.
    cycles = sorted({tuple(sorted((a, b))) for a, b in edges if (b, a) in edges})
    hubs = sorted(incoming, key=lambda k: -incoming[k])[:8]
    mermaid = ["graph LR"] + [f'  "{a}" --> "{b}"' for a, b in ordered[:60]]
    return {
        "project_id": project_id,
        "internal_edges": len(edges),
        "edges": [{"from": a, "to": b} for a, b in ordered[:max_edges]],
        "truncated": len(ordered) > max_edges,
        "most_depended_on": [{"module": h, "used_by": incoming[h]} for h in hubs],
        "two_way_dependencies": [list(c) for c in cycles],
        "top_external": sorted(({"name": k, "uses": v} for k, v in external.items()), key=lambda x: -x["uses"])[:15],
        "mermaid": "\n".join(mermaid),
        "confidence": "inferred (requires/imports resolved by file name and path)",
    }


# --------------------------------------------------------------------------
# Unused code, duplicates, complexity, API
# --------------------------------------------------------------------------

def unused(project_id: str, include_exported: bool = False, limit: int = 50) -> dict[str, Any]:
    root = _pid_or_error(project_id)
    s = get_storage()
    called = {r["name"] for r in s.query("SELECT DISTINCT name FROM code_calls WHERE project_id = ?", (project_id,))}
    # Names passed as values (callbacks: Connect(onTouched), addEventListener('x', onClick))
    # appear in the FTS text; keep any name mentioned more often than it is defined.
    rows = s.query("SELECT path, name, qualifier, kind, line_start, exported FROM code_defs WHERE project_id = ? AND kind != 'class' "
                   "AND kind NOT IN ('interface', 'enum', 'record', 'object') ORDER BY path, line_start", (project_id,))
    texts: dict[str, str] = {}
    findings = []
    for r in rows:
        name = r["name"]
        if name in called or name in ENTRY_POINTS or name.startswith(("on", "On", "test", "Test", "_")):
            continue
        if r["exported"] and not include_exported:
            continue
        path = r["path"]
        if path not in texts:
            try:
                texts[path] = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                texts[path] = ""
        mentions = len(re.findall(rf"(?<![\w$]){re.escape(name)}(?![\w$])", texts[path]))
        if mentions > 1:
            continue  # referenced as a value somewhere in its own file
        findings.append({"file": _rel(root, path), "line": r["line_start"], "name": name, "kind": r["kind"],
                         "exported": bool(r["exported"])})
    return {
        "project_id": project_id,
        "likely_unused": findings[:limit],
        "count": len(findings),
        "truncated": len(findings) > limit,
        "confidence": "inferred: no call or reference by name found in this project. Engine callbacks and exports are skipped"
                      + ("" if not include_exported else "; exports included, so check external users"),
    }


def duplicates(project_id: str, min_lines: int = 6, limit: int = 20) -> dict[str, Any]:
    root = _pid_or_error(project_id)
    s = get_storage()
    files = [r["path"] for r in s.query("SELECT path, language FROM files_index WHERE project_id = ?", (project_id,))
             if LANG_FAMILY.get(r["language"])]
    min_lines = max(3, min(int(min_lines), 50))
    seen: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        family = LANG_FAMILY.get(detect_language(Path(f)), "")
        for start, end, chunk in windows(normalized_lines(text, family), min_lines):
            seen[sha256_text(chunk)].append((f, start, end))
    # Pair up matching windows between two locations, then merge runs of
    # overlapping windows into one block per pair of locations.
    pair_hits: dict[tuple[str, str], list[tuple[int, int, int, int]]] = defaultdict(list)
    for spots in seen.values():
        uniq = sorted(set(spots))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                (fa, a1, a2), (fb, b1, b2) = uniq[i], uniq[j]
                if fa == fb and a1 <= b2 and b1 <= a2:
                    continue  # overlapping window in the same file
                pair_hits[(fa, fb)].append((a1, a2, b1, b2))
    result = []
    for (fa, fb), hits in pair_hits.items():
        hits.sort()
        cur = list(hits[0])
        for a1, a2, b1, b2 in hits[1:]:
            if a1 <= cur[1] + 1 and b1 <= cur[3] + 1 and b1 >= cur[2]:
                cur[1] = max(cur[1], a2)
                cur[3] = max(cur[3], b2)
            else:
                result.append((fa, fb, *cur))
                cur = [a1, a2, b1, b2]
        result.append((fa, fb, *cur))
    blocks = [{"occurrences": [{"file": _rel(root, fa), "lines": [a1, a2]}, {"file": _rel(root, fb), "lines": [b1, b2]}],
               "lines": max(a2 - a1, b2 - b1) + 1} for fa, fb, a1, a2, b1, b2 in result]
    blocks.sort(key=lambda g: (-g["lines"], g["occurrences"][0]["file"]))
    result = blocks
    return {"project_id": project_id, "min_lines": min_lines, "duplicate_blocks": len(result),
            "blocks": result[:limit], "truncated": len(result) > limit,
            "note": "Comments, strings, and whitespace are ignored when comparing."}


def complexity(project_id: str, top: int = 15) -> dict[str, Any]:
    root = _pid_or_error(project_id)
    rows = get_storage().query(
        "SELECT path, name, qualifier, kind, line_start, line_end, complexity, params FROM code_defs "
        "WHERE project_id = ? AND kind != 'class' ORDER BY complexity DESC, (line_end - line_start) DESC LIMIT ?",
        (project_id, max(1, min(int(top), 200))))
    def grade(c: int) -> str:
        return "simple" if c <= 5 else "moderate" if c <= 10 else "complex" if c <= 20 else "very complex"
    out = [{"file": _rel(root, r["path"]), "function": (r["qualifier"] + "." if r["qualifier"] else "") + r["name"],
            "line": r["line_start"], "length": r["line_end"] - r["line_start"] + 1, "complexity": r["complexity"],
            "grade": grade(r["complexity"]), "params": r["params"]} for r in rows]
    stats = get_storage().query(
        "SELECT COUNT(*) AS n, AVG(complexity) AS avg, MAX(complexity) AS mx FROM code_defs WHERE project_id = ? AND kind != 'class'",
        (project_id,))[0]
    return {"project_id": project_id, "functions": stats["n"], "average_complexity": round(stats["avg"] or 0, 2),
            "max_complexity": stats["mx"] or 0, "hotspots": out,
            "note": "Complexity counts branches (if/loops/and/or/case/catch); over 10 is worth splitting."}


def public_api(project_id: str, path_filter: str = "", limit: int = 100) -> dict[str, Any]:
    root = _pid_or_error(project_id)
    rows = get_storage().query(
        "SELECT path, name, qualifier, kind, line_start, params FROM code_defs WHERE project_id = ? AND exported = 1 "
        "ORDER BY path, line_start", (project_id,))
    if path_filter:
        rows = [r for r in rows if path_filter in r["path"]]
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_file[_rel(root, r["path"])].append({"name": (r["qualifier"] + "." if r["qualifier"] else "") + r["name"],
                                                "kind": r["kind"], "line": r["line_start"], "params": r["params"]})
    files = sorted(by_file)
    return {"project_id": project_id, "files": len(files), "symbols": len(rows),
            "api": {f: by_file[f] for f in files[:limit]}, "truncated": len(files) > limit}


def overview(project_id: str) -> dict[str, Any]:
    """Code health at a glance: sizes, hotspots, cycles, likely-unused, duplicates."""
    cx = complexity(project_id, top=5)
    graph = module_graph(project_id, max_edges=0)
    dead = unused(project_id, limit=5)
    dup = duplicates(project_id, limit=3)
    return {
        "project_id": project_id,
        "functions": cx["functions"],
        "average_complexity": cx["average_complexity"],
        "hotspots": cx["hotspots"],
        "internal_module_links": graph["internal_edges"],
        "most_depended_on": graph["most_depended_on"][:5],
        "two_way_dependencies": graph["two_way_dependencies"][:5],
        "likely_unused": dead["likely_unused"],
        "likely_unused_total": dead["count"],
        "duplicate_blocks": dup["duplicate_blocks"],
        "top_duplicates": dup["blocks"],
    }


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return {"status": "ok", "summary": summary, "data": fn(args)}
            except ValueError as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    specs = [
        ("code.overview", "Code health in one call: function count, complexity hotspots, module links, two-way dependencies, likely-unused code, duplicates.",
         {"project_id": "str"}, lambda a: overview(a["project_id"]), "Code overview ready."),
        ("code.callers", "Where a function is defined and every place that calls it, with the calling function's name (Luau, JS, Java, Kotlin, Python).",
         {"project_id": "str", "name": "str", "limit": "optional int"}, lambda a: callers(a["project_id"], a["name"], int(a.get("limit", 50))), "Callers found."),
        ("code.graph", "Module dependency graph from require/import: internal links, most-used modules, two-way dependencies, external packages, Mermaid text.",
         {"project_id": "str", "focus": "optional str", "max_edges": "optional int"},
         lambda a: module_graph(a["project_id"], a.get("focus", ""), int(a.get("max_edges", 200))), "Module graph built."),
        ("code.unused", "Functions with no call or reference in the project (engine callbacks and exports skipped by default).",
         {"project_id": "str", "include_exported": "optional bool", "limit": "optional int"},
         lambda a: unused(a["project_id"], a.get("include_exported", False), int(a.get("limit", 50))), "Unused-code check done."),
        ("code.duplicates", "Copy-pasted code blocks across files, ignoring comments, strings, and whitespace.",
         {"project_id": "str", "min_lines": "optional int", "limit": "optional int"},
         lambda a: duplicates(a["project_id"], int(a.get("min_lines", 6)), int(a.get("limit", 20))), "Duplicate check done."),
        ("code.complexity", "Most complex functions (branch count, length, parameters) with a plain grade.",
         {"project_id": "str", "top": "optional int"}, lambda a: complexity(a["project_id"], int(a.get("top", 15))), "Complexity ranked."),
        ("code.api", "Public interface per file: Luau module functions, JS exports, public Java/Kotlin members.",
         {"project_id": "str", "path_filter": "optional str", "limit": "optional int"},
         lambda a: public_api(a["project_id"], a.get("path_filter", ""), int(a.get("limit", 100))), "API listed."),
    ]
    for cid, summary, schema, fn, done in specs:
        reg.register(CapabilitySpec(id=cid, area="code", summary=summary, input_schema=schema, permission="read",
                                    handler=wrap(fn, done), tags=["code", "coding", "analysis", "luau", "javascript", "java"]))
