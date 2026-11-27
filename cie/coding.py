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


def codebase_brain(project_id: str = "") -> dict[str, Any]:
    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    storage = get_storage()
    summary_projects: list[dict[str, Any]] = []

    for r in roots:
        pid = r["id"]
        files = iter_project_files(r)
        lang_counts: dict[str, int] = {}
        total_symbols = 0

        def _tx(conn: Any) -> None:
            nonlocal total_symbols
            conn.execute("DELETE FROM symbols_index WHERE project_id = ?", (pid,))
            for f in files:
                lang = detect_language(f)
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
                if lang in {"python", "javascript", "typescript", "csharp", "cpp", "c", "rust", "go", "java", "gdscript"}:
                    try:
                        txt = f.read_text(encoding="utf-8", errors="replace")
                        syms = extract_symbols(f, txt)
                        for s in syms:
                            conn.execute(
                                "INSERT INTO symbols_index(file_path, project_id, name, kind, line_start, line_end) VALUES (?, ?, ?, ?, ?, ?)",
                                (str(f), pid, s["name"], s["kind"], s["line_start"], s["line_end"]),
                            )
                            total_symbols += 1
                    except Exception:
                        pass

        storage.run_write(_tx)
        summary_projects.append({
            "project_id": pid,
            "file_count": len(files),
            "languages": lang_counts,
            "symbols_indexed": total_symbols,
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


def dependency_mapper(project_id: str = "") -> dict[str, Any]:
    roots = project_roots()
    if project_id:
        roots = [r for r in roots if r["id"] == project_id]
    edges: list[dict[str, str]] = []
    for r in roots:
        for f in iter_project_files(r):
            lang = detect_language(f)
            if lang == "python":
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                edges.append({"source": str(f), "target": alias.name, "kind": "import", "confidence": "exact"})
                        elif isinstance(node, ast.ImportFrom) and node.module:
                            edges.append({"source": str(f), "target": node.module, "kind": "from_import", "confidence": "exact"})
                except SyntaxError:
                    pass
            elif lang in {"javascript", "typescript", "csharp", "cpp", "c", "rust", "go", "java"}:
                for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()[:100]:
                    m = IMPORT_RE.match(ln)
                    if m:
                        edges.append({"source": str(f), "target": m.group(1), "kind": "import", "confidence": "inferred"})
    return {"edge_count": len(edges), "edges": edges[:300]}


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
        input_schema={"project_id": "optional str"},
        permission="read",
        handler=lambda args: {"status": "ok", "summary": "Dependency map built.", "data": dependency_mapper(args.get("project_id", ""))},
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