"""Coding intelligence subsystem: Codebase Brain, multi-lang symbol parsing, dependencies, stack traces, smart tests, security checks."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import iter_project_files, load_config, project_roots, redact_secrets, resolve_in_projects
from .efficiency import detect_language, errors_only
from .storage import get_storage
from .vault import save_note

GENERIC_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|struct|enum|fn|def|func)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
IMPORT_RE = re.compile(r"^\s*(?:import|from|#include|using|use)\s+([^\s;'\"]+)")
LUA_DEF_RE = re.compile(r"^\s*(?:local\s+)?function\s+([A-Za-z_][A-Za-z0-9_.:]*)\s*\(")
LUA_ASSIGN_FN_RE = re.compile(r"^\s*(?:local\s+)?([A-Za-z_][A-Za-z0-9_.:]*)\s*=\s*function\s*\(")
LUA_REQUIRE_RE = re.compile(r"\brequire\s*\(\s*([^)]+?)\s*\)")
JS_FROM_RE = re.compile(r"""^\s*(?:import|export)\b[^'"]*?\bfrom\s*['"]([^'"]+)['"]""")
JS_BARE_IMPORT_RE = re.compile(r"""^\s*import\s*['"]([^'"]+)['"]""")
JS_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")
INCLUDE_RE = re.compile(r"""^\s*#include\s*[<"]([^>"]+)[>"]""")
TRACE_LINE_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)')
GENERIC_TRACE_RE = re.compile(r"([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+):(\d+)")

SECURITY_RULES = [
    ("hardcoded_secret", re.compile(r"(?i)(api_key|secret|password|token)\s*=\s*['\"][^'\"]{8,}['\"]"), "high"),
    ("dynamic_exec", re.compile(r"\b(eval|exec)\s*\("), "medium"),
    ("shell_true", re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "high"),
    ("sql_interpolation", re.compile(r"execute\(\s*f['\"]"), "medium"),
]


def extract_symbols(path: Path, text: str | None = None) -> list[dict[str, Any]]:
    content = text if text is not None else path.read_text(encoding="utf-8", errors="replace")
    lang = detect_language(path)
    symbols: list[dict[str, Any]] = []

    if lang == "python":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    symbols.append({
                        "name": node.name,
                        "kind": kind,
                        "line_start": getattr(node, "lineno", 1),
                        "line_end": getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                        "parser": "python_ast",
                    })
            symbols.sort(key=lambda s: s["line_start"])
            return symbols
        except SyntaxError:
            pass

    if lang in {"lua", "luau"}:
        for idx, line in enumerate(content.splitlines(), start=1):
            m = LUA_DEF_RE.match(line) or LUA_ASSIGN_FN_RE.match(line)
            if m:
                symbols.append({
                    "name": m.group(1),
                    "kind": "function",
                    "line_start": idx,
                    "line_end": idx,
                    "parser": "lua_regex",
                })
        return symbols

    for idx, line in enumerate(content.splitlines(), start=1):
        m = GENERIC_DEF_RE.match(line)
        if m:
            symbols.append({
                "name": m.group(1),
                "kind": "symbol",
                "line_start": idx,
                "line_end": idx,
                "parser": "regex_fallback",
            })
    return symbols


def extract_dependencies(path: Path, text: str | None = None) -> list[dict[str, str]]:
    """Return import/require edges for one file. Python uses the AST (exact);
    other languages use line patterns (inferred)."""
    content = text if text is not None else path.read_text(encoding="utf-8", errors="replace")
    lang = detect_language(path)
    edges: list[dict[str, str]] = []
    if lang == "python":
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return edges
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append({"target": alias.name, "kind": "import", "confidence": "exact"})
            elif isinstance(node, ast.ImportFrom) and node.module:
                edges.append({"target": node.module, "kind": "from_import", "confidence": "exact"})
        return edges
    if lang in {"lua", "luau"}:
        # Balance parentheses so nested calls such as
        # require(game:GetService("ReplicatedStorage"):WaitForChild("Util")) are kept whole.
        from .codescan import mask

        masked = mask(content, "lua")
        for m in re.finditer(r"\brequire\s*\(", masked):
            depth, j = 1, m.end()
            while j < len(masked) and depth:
                if masked[j] == "(":
                    depth += 1
                elif masked[j] == ")":
                    depth -= 1
                j += 1
            target = content[m.end():j - 1].strip().strip("'\" ")
            if target:
                edges.append({"target": target, "kind": "require", "confidence": "inferred"})
        return edges
    for line in content.splitlines():
        if lang in {"javascript", "typescript"}:
            m = JS_FROM_RE.match(line) or JS_BARE_IMPORT_RE.match(line)
            if m:
                edges.append({"target": m.group(1), "kind": "import", "confidence": "inferred"})
            for r in JS_REQUIRE_RE.finditer(line):
                edges.append({"target": r.group(1), "kind": "require", "confidence": "inferred"})
            continue
        if lang in {"c", "cpp"}:
            m = INCLUDE_RE.match(line)
            if m:
                edges.append({"target": m.group(1), "kind": "include", "confidence": "inferred"})
            continue
        if lang in {"csharp", "rust", "go", "java", "kotlin"}:
            m = IMPORT_RE.match(line)
            if m:
                edges.append({"target": m.group(1), "kind": "import", "confidence": "inferred"})
    return edges


def codebase_brain(project_id: str = "") -> dict[str, Any]:
    """Project map read from the unified index (refreshed incrementally first),
    so its counts always match cie_status and coding.dependencies."""
    from .efficiency import incremental_index

    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    storage = get_storage()
    summary_projects: list[dict[str, Any]] = []
    for r in roots:
        pid = r["id"]
        refresh = incremental_index(pid)
        langs = storage.query(
            "SELECT language, COUNT(*) AS c FROM files_index WHERE project_id = ? GROUP BY language ORDER BY c DESC",
            (pid,),
        )
        files = sum(row["c"] for row in langs)
        symbols = storage.query("SELECT COUNT(*) AS c FROM symbols_index WHERE project_id = ?", (pid,))[0]["c"]
        deps = storage.query("SELECT COUNT(*) AS c FROM dependencies_index WHERE project_id = ?", (pid,))[0]["c"]
        summary_projects.append({
            "project_id": pid,
            "file_count": files,
            "languages": {row["language"]: row["c"] for row in langs},
            "symbols_indexed": symbols,
            "dependencies_indexed": deps,
            "refresh": refresh,
        })
    return {"projects": summary_projects}


def symbol_search(query: str, project_id: str = "", limit: int = 25) -> dict[str, Any]:
    storage = get_storage()
    sql = "SELECT file_path, project_id, name, kind, line_start, line_end FROM symbols_index WHERE LOWER(name) LIKE LOWER(?)"
    params: list[Any] = [f"%{query.strip()}%"]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY name LIMIT ?"
    params.append(limit)
    rows = storage.query(sql, tuple(params))
    if not rows:
        codebase_brain(project_id)
        rows = storage.query(sql, tuple(params))
    return {"matches": rows, "total": len(rows)}


def read_symbol(path: str, symbol_name: str) -> dict[str, Any]:
    resolved, _ = resolve_in_projects(path)
    lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    syms = extract_symbols(resolved, "\n".join(lines))
    for s in syms:
        if s["name"] == symbol_name:
            start = max(1, s["line_start"])
            end = min(len(lines), max(start, s["line_end"]))
            return {
                "path": str(resolved),
                "symbol": s,
                "source": "\n".join(lines[start - 1:end]),
            }
    return {"path": str(resolved), "symbol": None, "source": "", "warning": f"Symbol '{symbol_name}' not found."}


def dependency_mapper(project_id: str = "", max_edges: int = 100) -> dict[str, Any]:
    """Dependency map read from the unified index. Returns a compact summary
    (top targets, confidence counts) plus at most ``max_edges`` raw edges."""
    from .efficiency import incremental_index

    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    pids = [r["id"] for r in roots]
    for pid in pids:
        incremental_index(pid)
    if not pids:
        return {"edge_count": 0, "edges": [], "truncated": False, "omitted": 0, "top_targets": [], "by_confidence": {}}
    storage = get_storage()
    marks = ",".join("?" for _ in pids)
    params = tuple(pids)
    total = storage.query(f"SELECT COUNT(*) AS c FROM dependencies_index WHERE project_id IN ({marks})", params)[0]["c"]
    by_conf = storage.query(
        f"SELECT confidence, COUNT(*) AS c FROM dependencies_index WHERE project_id IN ({marks}) GROUP BY confidence", params
    )
    top = storage.query(
        f"SELECT target, COUNT(*) AS c FROM dependencies_index WHERE project_id IN ({marks}) GROUP BY target ORDER BY c DESC, target LIMIT 25",
        params,
    )
    limit = max(0, min(int(max_edges), 2000))
    rows = storage.query(
        f"SELECT source_file AS source, target, kind, confidence, project_id FROM dependencies_index WHERE project_id IN ({marks}) ORDER BY source_file, id LIMIT ?",
        params + (limit,),
    )
    return {
        "edge_count": total,
        "by_confidence": {r["confidence"]: r["c"] for r in by_conf},
        "top_targets": [{"target": r["target"], "count": r["c"]} for r in top],
        "edges": rows,
        "truncated": total > len(rows),
        "omitted": max(0, total - len(rows)),
    }


def stack_trace_mapper(traceback_text: str) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    for m in TRACE_LINE_RE.finditer(traceback_text):
        frames.append({"file": m.group(1), "line": int(m.group(2))})
    if not frames:
        for m in GENERIC_TRACE_RE.finditer(traceback_text):
            frames.append({"file": m.group(1), "line": int(m.group(2))})
    mapped: list[dict[str, Any]] = []
    for fr in frames[:20]:
        entry: dict[str, Any] = dict(fr)
        try:
            resolved, _ = resolve_in_projects(fr["file"])
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            ln = fr["line"]
            if 1 <= ln <= len(lines):
                entry["code_line"] = lines[ln - 1].strip()
                syms = [s for s in extract_symbols(resolved) if s["line_start"] <= ln <= s["line_end"]]
                entry["symbol"] = syms[-1]["name"] if syms else "module"
        except Exception:
            entry["symbol"] = "external_or_unmapped"
        mapped.append(entry)
    return {"frames": mapped}


def discover_tests(project_id: str = "") -> dict[str, Any]:
    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    tests: list[dict[str, str]] = []
    for r in roots:
        for f in iter_project_files(r):
            name = f.name.lower()
            if name.startswith("test_") or name.endswith("_test.py") or ".test." in name or ".spec." in name:
                tests.append({"project_id": r["id"], "path": str(f), "framework": "python_test" if f.suffix == ".py" else "js_test"})
    return {"test_files": tests, "total": len(tests)}


def smart_tests(changed_files: list[str], project_id: str = "") -> dict[str, Any]:
    all_tests = discover_tests(project_id).get("test_files", [])
    changed_stems = {Path(c).stem.lower().replace("test_", "") for c in changed_files}
    selected = []
    for t in all_tests:
        t_stem = Path(t["path"]).stem.lower()
        if any(st and st in t_stem for st in changed_stems):
            selected.append({**t, "reason": "filename_or_module_match", "confidence": "inferred"})
    return {
        "selected_tests": selected,
        "total_discovered": len(all_tests),
        "coverage_uncertainty": "Static dependency selection may miss dynamic/integration tests; run full suite before release." if len(selected) < len(all_tests) else "All discovered tests selected.",
    }


def run_allowlisted_tests(command_key: str = "unittest", project_id: str = "", timeout_sec: int = 30) -> dict[str, Any]:
    cfg = load_config()
    allowlist = cfg.get("allowlisted_commands") or {}
    if command_key not in allowlist:
        raise PermissionError(f"Command '{command_key}' is not in allowlisted_commands.")
    roots = project_roots(cfg)
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    if not roots:
        raise PermissionError("No approved project folder to run tests in.")
    cwd = Path(roots[0]["path"]).resolve()
    cmd = list(allowlist[command_key])
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=min(120, max(5, int(timeout_sec))),
        shell=False,
    )
    combined = redact_secrets((proc.stdout or "") + "\n" + (proc.stderr or ""))
    return {
        "command": cmd,
        "cwd": str(cwd),
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "compressed_errors": errors_only(combined, max_items=30),
        "tail": combined.splitlines()[-25:],
    }


def security_checks(project_id: str = "") -> dict[str, Any]:
    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    findings: list[dict[str, Any]] = []
    for r in roots:
        for f in iter_project_files(r, extensions={".py", ".js", ".ts", ".jsx", ".tsx"}):
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for idx, ln in enumerate(lines, start=1):
                for rule_id, pat, sev in SECURITY_RULES:
                    if pat.search(ln):
                        findings.append({
                            "rule": rule_id,
                            "severity": sev,
                            "file": str(f),
                            "line": idx,
                        })
    return {
        "disclaimer": "First-pass static pattern review only; not a formal security audit.",
        "finding_count": len(findings),
        "findings": findings[:100],
    }


def record_fix(project: str, error_sig: str, fix_summary: str, verified: bool = False) -> dict[str, Any]:
    title = f"Fix - {error_sig[:50]}"
    body = f"# {title}\n\n- **Error Signature:** `{error_sig}`\n- **Fix Applied:** {fix_summary}\n- **Verified:** {'Yes' if verified else 'No'}\n"
    return save_note(
        title=title,
        body=body,
        folder="Fixes",
        project=project,
        tags=["fix", project] if project else ["fix"],
        source_type="cie_observed",
        confidence="verified" if verified else "observed",
    )


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="coding.brain",
        area="coding",
        summary="Build project map of languages, files, and indexed symbols.",
        input_schema={"project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Codebase Brain updated.", "data": codebase_brain(args.get("project_id", ""))},
        tags=["coding", "ast", "symbols"],
    ))
    reg.register(CapabilitySpec(
        id="coding.symbol_search",
        area="coding",
        summary="Search indexed functions, classes, and symbols across approved projects.",
        input_schema={"query": "str", "project_id": "optional str", "limit": "optional int"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Symbol search completed.", "data": symbol_search(args.get("query", ""), args.get("project_id", ""), int(args.get("limit", 25)))},
        tags=["coding", "symbol"],
    ))
    reg.register(CapabilitySpec(
        id="coding.read_symbol",
        area="coding",
        summary="Read the exact source body of a named function or class.",
        input_schema={"path": "str", "symbol_name": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Symbol source read.", "data": read_symbol(args["path"], args["symbol_name"])},
        tags=["coding", "symbol"],
    ))
    reg.register(CapabilitySpec(
        id="coding.dependencies",
        area="coding",
        summary="Map imports and dependencies with exact/inferred confidence labels.",
        input_schema={"project_id": "optional str", "max_edges": "optional int 0-2000"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Dependency map built.", "data": dependency_mapper(args.get("project_id", ""), int(args.get("max_edges", 100)))},
        tags=["coding", "dependencies"],
    ))
    reg.register(CapabilitySpec(
        id="coding.stack_trace",
        area="coding",
        summary="Map stack-trace file/line frames to local symbols and code lines.",
        input_schema={"traceback_text": "str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Stack trace mapped.", "data": stack_trace_mapper(args.get("traceback_text", ""))},
        tags=["coding", "debug"],
    ))
    reg.register(CapabilitySpec(
        id="coding.discover_tests",
        area="coding",
        summary="Discover test files without executing them.",
        input_schema={"project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Tests discovered.", "data": discover_tests(args.get("project_id", ""))},
        tags=["coding", "tests"],
    ))
    reg.register(CapabilitySpec(
        id="coding.smart_tests",
        area="coding",
        summary="Select relevant tests for changed files with uncertainty notice.",
        input_schema={"changed_files": "list[str]", "project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Smart test selection completed.", "data": smart_tests(args.get("changed_files") or [], args.get("project_id", ""))},
        tags=["coding", "tests"],
    ))
    reg.register(CapabilitySpec(
        id="coding.run_tests",
        area="coding",
        summary="Run an allowlisted test command and return compressed error output.",
        input_schema={"command_key": "str", "project_id": "optional str", "timeout_sec": "optional int"},
        permission="exec",
        handler=lambda args: {"status": "ok", "summary": "Allowlisted test run finished.", "data": run_allowlisted_tests(args.get("command_key", "unittest"), args.get("project_id", ""), int(args.get("timeout_sec", 30)))},
        tags=["coding", "tests", "exec"],
    ))
    reg.register(CapabilitySpec(
        id="coding.security_checks",
        area="coding",
        summary="Run first-pass static security pattern checks on project source files.",
        input_schema={"project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "First-pass security checks completed.", "data": security_checks(args.get("project_id", ""))},
        tags=["coding", "security"],
    ))
    reg.register(CapabilitySpec(
        id="coding.record_fix",
        area="coding",
        summary="Record an error signature and verified fix into the Obsidian Fixes folder.",
        input_schema={"project": "str", "error_sig": "str", "fix_summary": "str", "verified": "optional bool"},
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Fix recorded in vault.", "data": record_fix(args.get("project", ""), args.get("error_sig", ""), args.get("fix_summary", ""), bool(args.get("verified", False)))},
        tags=["coding", "memory", "fix"],
    ))