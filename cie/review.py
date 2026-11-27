"""Phase 2.2 code review with approve-to-apply fixes.

Rules target real problems in the owner's stack: Roblox Luau, browser/Hearth
JavaScript, and Minecraft Java/Kotlin mods. Rules run on masked source (see
cie.codescan.mask), so comments and strings never trigger them.

Mechanical fixes become line-level edits. code.fix_preview shows them as a
unified diff; code.fix_apply writes only the fixes the owner picked, only if
each line is unchanged since the preview, and snapshots the files first.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .codescan import LANG_FAMILY, mask
from .config import project_roots, resolve_in_projects
from .efficiency import detect_language, incremental_index
from .storage import get_storage

ROBLOX_SERVICES = (
    "Players", "ReplicatedStorage", "ServerStorage", "ServerScriptService", "Workspace", "Lighting",
    "RunService", "TweenService", "UserInputService", "SoundService", "Debris", "HttpService",
    "DataStoreService", "MarketplaceService", "CollectionService", "PathfindingService", "TeleportService",
    "StarterGui", "StarterPlayer", "Teams", "ContextActionService", "TextChatService",
)


@dataclass
class Rule:
    id: str
    family: str
    severity: str  # error | warning | info
    message: str
    check: Callable[[str, str, list[str], str], list[tuple[int, str | None]]]
    # check(text, masked, lines, path) -> [(line_number, fixed_line_or_None)]


def _line_hits(pattern: str, masked: str, lines: list[str], fixer: Callable[[str], str] | None = None,
               skip: Callable[[str, str], bool] | None = None) -> list[tuple[int, str | None]]:
    rx = re.compile(pattern)
    out = []
    for i, mline in enumerate(masked.split("\n"), start=1):
        if rx.search(mline):
            orig = lines[i - 1] if i - 1 < len(lines) else ""
            if skip and skip(mline, orig):
                continue
            fixed = fixer(orig) if fixer else None
            out.append((i, fixed if fixed != orig else None))
    return out


def _sub_outside_strings(line: str, pattern: str, repl: str, family: str) -> str:
    """Apply a regex replacement only where the masked line matches, so text
    inside strings and comments is left alone."""
    masked = mask(line, family)
    out, last = [], 0
    for m in re.finditer(pattern, masked):
        out.append(line[last:m.start()])
        out.append(m.expand(repl))
        last = m.end()
    out.append(line[last:])
    return "".join(out)


# ---------------------------- Luau -----------------------------------------

def _luau_strict(text, masked, lines, path):
    first = next((ln for ln in lines if ln.strip()), "")
    if first.strip().startswith("--!"):
        return []
    return [(1, "--!strict\n" + (lines[0] if lines else ""))]


def _luau_wait(text, masked, lines, path):
    return _line_hits(r"(?<![\w.:])wait\s*\(", masked, lines,
                      lambda ln: _sub_outside_strings(ln, r"(?<![\w.:])wait(\s*\()", r"task.wait\1", "lua"))


def _luau_spawn_delay(text, masked, lines, path):
    return _line_hits(r"(?<![\w.:])(spawn|delay)\s*\(", masked, lines,
                      lambda ln: _sub_outside_strings(ln, r"(?<![\w.:])(spawn|delay)(\s*\()", r"task.\1\2", "lua"))


def _luau_service(text, masked, lines, path):
    names = "|".join(ROBLOX_SERVICES)
    return _line_hits(rf"\bgame\.({names})\b", masked, lines,
                      lambda ln: _sub_outside_strings(ln, rf"\bgame\.({names})\b", r'game:GetService("\1")', "lua"))


def _luau_while_no_yield(text, masked, lines, path):
    out = []
    mlines = masked.split("\n")
    for i, ml in enumerate(mlines):
        if re.search(r"\bwhile\s+true\s+do\b", ml):
            body = "\n".join(mlines[i:i + 40])
            depth, seen_end = 0, False
            for tok in re.finditer(r"\b(do|then|function|repeat|end|until)\b", body):
                depth += 1 if tok.group(1) in {"do", "then", "function", "repeat"} else -1
                if depth == 0:
                    body = body[:tok.end()]
                    seen_end = True
                    break
            if seen_end and not re.search(r"\b(wait|Wait|yield|Heartbeat|Stepped|RenderStepped|PreSimulation)\b", body):
                out.append((i + 1, None))
    return out


def _luau_remote_unvalidated(text, masked, lines, path):
    from .codescan import _lua_end

    out = []
    for m in re.finditer(r"OnServerEvent\s*:\s*Connect\s*\(\s*function\s*\(\s*\w+\s*(,\s*[^)]+)?\)", masked):
        if not m.group(1):
            continue  # handler takes only the player
        fstart = masked.find("function", m.start())
        body = masked[fstart:_lua_end(masked, fstart)]  # just this handler
        if not re.search(r"\btypeof\s*\(|\btype\s*\(|\bt\.\w+\(|\bassert\s*\(|IsA\s*\(", body):
            out.append((masked.count("\n", 0, m.start()) + 1, None))
    return out


# ---------------------------- JavaScript -----------------------------------

_LOOSE = r"(?<![=!<>])(==|!=)(?!=)(?!\s*(?:null|undefined)\b)"


def _js_loose_eq(text, masked, lines, path):
    """Flag == / != except comparisons with null/undefined (an accepted idiom)."""
    out = []
    for i, mline in enumerate(masked.split("\n"), start=1):
        hits = [m for m in re.finditer(_LOOSE, mline)
                if not re.search(r"\b(null|undefined)\s*$", mline[:m.start()])]
        if not hits:
            continue
        orig = lines[i - 1]
        fixed, last = [], 0
        for m in hits:
            fixed.append(orig[last:m.end()])
            fixed.append("=")
            last = m.end()
        fixed.append(orig[last:])
        out.append((i, "".join(fixed)))
    return out


def _js_var(text, masked, lines, path):
    return _line_hits(r"(?<![\w$.])var\s+", masked, lines,
                      lambda ln: _sub_outside_strings(ln, r"(?<![\w$.])var(\s+)", r"let\1", "js"))


def _js_inner_html(text, masked, lines, path):
    out = []
    for n, _ in _line_hits(r"\.(innerHTML|outerHTML)\s*\+?=", masked, lines):
        rhs = re.split(r"\.(?:innerHTML|outerHTML)\s*\+?=", lines[n - 1], maxsplit=1)[-1].strip().rstrip(";").strip()
        if re.fullmatch(r"([\"'])[^\"']*\1", rhs) or (rhs.startswith("`") and "${" not in rhs):
            continue  # a fixed string is safe
        out.append((n, None))
    return out


def _js_eval(text, masked, lines, path):
    return _line_hits(r"(?<![\w$.])eval\s*\(|\bnew\s+Function\s*\(", masked, lines)


def _js_unsafe_parse(text, masked, lines, path):
    out = []
    mlines = masked.split("\n")
    for i, ml in enumerate(mlines):
        if re.search(r"JSON\.parse\s*\(\s*localStorage\.getItem", ml):
            window = "\n".join(mlines[max(0, i - 4):i + 1])
            if not re.search(r"\btry\b", window):
                out.append((i + 1, None))
    return out


def _js_hearth_unguarded(text, masked, lines, path):
    return _line_hits(r"(?<![\w$.])Hearth\.unlock\s*\(", masked, lines,
                      lambda ln: (re.sub(r"^(\s*)(Hearth\.unlock\s*\([^;]*\)\s*;?)", r"\1if (window.Hearth) \2", ln)),
                      skip=lambda ml, orig: "window.Hearth" in ml or "?." in ml)


def _js_hearth_storage(text, masked, lines, path):
    if not re.search(r"(^|/)games/", path.replace("\\", "/")):
        return []
    return _line_hits(r"\bindexedDB\b|document\.cookie", masked, lines)


def _js_set_interval_loop(text, masked, lines, path):
    if "requestAnimationFrame" in masked:
        return []
    hits = _line_hits(r"\bsetInterval\s*\(\s*\w+\s*,\s*(1[0-9]|[1-9]|3[0-3]|16\.\d+|1000\s*/\s*60)\s*\)", masked, lines)
    return hits


# ---------------------------- Java / Kotlin --------------------------------

def _java_string_eq(text, masked, lines, path):
    return _line_hits(r"==\s*\"|\"\s*==|!=\s*\"|\"\s*!=", masked, lines)


def _java_empty_catch(text, masked, lines, path):
    out = []
    for m in re.finditer(r"catch\s*\([^)]*\)\s*\{\s*\}", masked):
        out.append((masked.count("\n", 0, m.start()) + 1, None))
    return out


def _java_print(text, masked, lines, path):
    return _line_hits(r"System\.(out|err)\.print|\.printStackTrace\s*\(\s*\)", masked, lines)


def _mc_client_import(text, masked, lines, path):
    p = path.replace("\\", "/").lower()
    if "/client/" in p or "client" in Path(p).stem:
        return []
    return _line_hits(r"^\s*import\s+net\.minecraft\.client\.", masked, lines)


def _kotlin_bang(text, masked, lines, path):
    return _line_hits(r"!!(?!=)", masked, lines)


RULES: list[Rule] = [
    Rule("luau.strict-mode", "lua", "info", "Add --!strict at the top so Luau type-checks this script.", _luau_strict),
    Rule("luau.deprecated-wait", "lua", "warning", "wait() is deprecated and throttled; use task.wait().", _luau_wait),
    Rule("luau.deprecated-spawn", "lua", "warning", "spawn()/delay() are deprecated; use task.spawn()/task.delay().", _luau_spawn_delay),
    Rule("luau.get-service", "lua", "info", "Use game:GetService(\"...\") instead of game.Service; it works even if the service was renamed or not created yet.", _luau_service),
    Rule("luau.loop-without-yield", "lua", "error", "while true do loop never yields; it will freeze the game. Add task.wait() or use RunService.", _luau_while_no_yield),
    Rule("luau.remote-unvalidated", "lua", "warning", "OnServerEvent handler uses client values without checking them (typeof/IsA/assert). Exploiters can send anything.", _luau_remote_unvalidated),
    Rule("js.loose-equality", "js", "warning", "Use === / !== to avoid type-coercion bugs.", _js_loose_eq),
    Rule("js.var", "js", "info", "Use let or const instead of var (block scoping).", _js_var),
    Rule("js.inner-html", "js", "warning", "Assigning built strings to innerHTML can inject HTML; use textContent or build elements.", _js_inner_html),
    Rule("js.eval", "js", "error", "eval()/new Function() run arbitrary code; avoid them.", _js_eval),
    Rule("js.unsafe-save-parse", "js", "warning", "JSON.parse(localStorage.getItem(...)) throws on a corrupt save; wrap it in try/catch.", _js_unsafe_parse),
    Rule("hearth.unguarded-unlock", "js", "warning", "Guard achievements with if (window.Hearth) so the game also runs outside Hearth.", _js_hearth_unguarded),
    Rule("hearth.storage", "js", "warning", "Hearth games save with localStorage only (no IndexedDB or cookies).", _js_hearth_storage),
    Rule("js.interval-game-loop", "js", "info", "Use requestAnimationFrame for the game loop instead of setInterval (smoother, pauses in background tabs).", _js_set_interval_loop),
    Rule("java.string-equality", "java", "error", "Compare strings with .equals(), not == / !=.", _java_string_eq),
    Rule("java.empty-catch", "java", "warning", "Empty catch block hides errors; log or handle them.", _java_empty_catch),
    Rule("mc.print-debug", "java", "info", "Use the mod's LOGGER instead of System.out / printStackTrace.", _java_print),
    Rule("mc.client-import-in-common", "java", "error", "Common/server code imports net.minecraft.client; this crashes dedicated servers. Move it to client-only code.", _mc_client_import),
    Rule("kotlin.not-null-assert", "kotlin", "info", "!! throws on null; prefer ?. , ?: or requireNotNull with a message.", _kotlin_bang),
    Rule("kotlin.string-print", "kotlin", "info", "Use the mod's LOGGER instead of println.", lambda t, m, l, p: _line_hits(r"(?<![\w.])println\s*\(", m, l)),
    Rule("mc.client-import-in-common-kt", "kotlin", "error", "Common/server code imports net.minecraft.client; this crashes dedicated servers.", _mc_client_import),
    Rule("java.empty-catch-kt", "kotlin", "warning", "Empty catch block hides errors.", _java_empty_catch),
]
RULE_IDS = {r.id for r in RULES}


def review_text(text: str, language: str, path: str = "", rules: set[str] | None = None) -> list[dict[str, Any]]:
    family = LANG_FAMILY.get(language, "")
    if not family or family == "python":
        return []
    masked = mask(text, family)
    lines = text.split("\n")
    findings = []
    for rule in RULES:
        if rule.family != family or (rules and rule.id not in rules):
            continue
        for line, fixed in rule.check(text, masked, lines, path):
            orig = lines[line - 1] if 0 < line <= len(lines) else ""
            f: dict[str, Any] = {"rule": rule.id, "severity": rule.severity, "line": line,
                                 "message": rule.message, "code": orig.strip()[:160]}
            if fixed is not None:
                f["fix"] = {"old": orig, "new": fixed}
            findings.append(f)
    findings.sort(key=lambda f: (f["line"], f["rule"]))
    return findings


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _files(project_id: str, paths: list[str] | None) -> tuple[dict[str, Any], list[Path]]:
    roots = [r for r in project_roots() if r["id"] == project_id]
    if not roots:
        raise ValueError(f"No registered project with id '{project_id}'.")
    if paths:
        files = []
        for p in paths:
            resolved, _ = resolve_in_projects(p)
            files.append(resolved)
        return roots[0], files
    incremental_index(project_id)
    rows = get_storage().query("SELECT path, language FROM files_index WHERE project_id = ?", (project_id,))
    return roots[0], [Path(r["path"]) for r in rows if LANG_FAMILY.get(r["language"]) not in (None, "python")]


def _rel(root: dict[str, Any], path: Path) -> str:
    try:
        return path.relative_to(root["path"]).as_posix()
    except ValueError:
        return str(path)


def review(project_id: str, paths: list[str] | None = None, rules: list[str] | None = None,
           min_severity: str = "info", limit: int = 100) -> dict[str, Any]:
    if rules and set(rules) - RULE_IDS:
        raise ValueError(f"Unknown rule(s): {sorted(set(rules) - RULE_IDS)}")
    root, files = _files(project_id, paths)
    cutoff = SEVERITY_ORDER.get(min_severity, 2)
    out, counts = [], {"error": 0, "warning": 0, "info": 0}
    by_rule: dict[str, int] = {}
    fixable = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for item in review_text(text, detect_language(f), str(f), set(rules) if rules else None):
            if SEVERITY_ORDER[item["severity"]] > cutoff:
                continue
            counts[item["severity"]] += 1
            by_rule[item["rule"]] = by_rule.get(item["rule"], 0) + 1
            rel = _rel(root, f)
            item["file"] = rel
            if "fix" in item:
                fixable += 1
                item["fix_id"] = f"{rel}:{item['line']}:{item['rule']}"
                item.pop("fix")
            out.append(item)
    out.sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], x["file"], x["line"]))
    return {"project_id": project_id, "files_checked": len(files), "counts": counts, "by_rule": by_rule,
            "fixable": fixable, "findings": out[:limit], "truncated": len(out) > limit,
            "next": "code.fix_preview shows the diff for fixable findings; code.fix_apply applies the ones you approve."}


def _collect_fixes(project_id: str, fix_ids: list[str] | None, paths: list[str] | None,
                   rules: list[str] | None) -> tuple[dict[str, Any], dict[Path, list[dict[str, Any]]]]:
    root, files = _files(project_id, paths)
    wanted = set(fix_ids or [])
    per_file: dict[Path, list[dict[str, Any]]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel(root, f)
        for item in review_text(text, detect_language(f), str(f), set(rules) if rules else None):
            if "fix" not in item:
                continue
            fid = f"{rel}:{item['line']}:{item['rule']}"
            if wanted and fid not in wanted:
                continue
            per_file.setdefault(f, []).append({**item, "fix_id": fid})
    return root, per_file


def _apply_to_lines(lines: list[str], fixes: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """Apply fixes line by line. Several fixes on one line combine: in-place
    edits run first (each must still see its original text), then inserts
    above the line (like adding --!strict) are added on top."""
    applied, stale = [], []
    new = list(lines)
    by_line: dict[int, list[dict[str, Any]]] = {}
    for fx in fixes:
        by_line.setdefault(fx["line"], []).append(fx)
    for line_no, group in by_line.items():
        i = line_no - 1
        if not 0 <= i < len(new):
            stale.extend(fx["fix_id"] for fx in group)
            continue
        original = new[i]
        current = original
        inserts = [fx for fx in group if fx["fix"]["new"].endswith("\n" + fx["fix"]["old"])]
        edits = [fx for fx in group if fx not in inserts]
        for fx in edits:
            old, repl = fx["fix"]["old"], fx["fix"]["new"]
            if old != original:
                stale.append(fx["fix_id"])
                continue
            if current == original:
                current = repl
            else:
                # Re-run this fix's transformation on the already-edited line.
                prefix = len(os.path.commonprefix([old, repl]))
                suffix = len(os.path.commonprefix([old[prefix:][::-1], repl[prefix:][::-1]]))
                removed, added = old[prefix:len(old) - suffix], repl[prefix:len(repl) - suffix]
                if removed and removed in current:
                    current = current.replace(removed, added, 1)
                elif not removed:
                    current = current[:prefix] + added + current[prefix:]
                else:
                    stale.append(fx["fix_id"])
                    continue
            applied.append(fx["fix_id"])
        for fx in inserts:
            if fx["fix"]["old"] != original:
                stale.append(fx["fix_id"])
                continue
            head = fx["fix"]["new"][: -len(fx["fix"]["old"])] if fx["fix"]["old"] else fx["fix"]["new"]
            current = head + current
            applied.append(fx["fix_id"])
        new[i] = current
    return new, applied, stale


def fix_preview(project_id: str, fix_ids: list[str] | None = None, paths: list[str] | None = None,
                rules: list[str] | None = None, max_lines: int = 400) -> dict[str, Any]:
    root, per_file = _collect_fixes(project_id, fix_ids, paths, rules)
    diffs, total, ids = [], 0, []
    for f, fixes in sorted(per_file.items()):
        lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
        new, applied, _ = _apply_to_lines(lines, fixes)
        rel = _rel(root, f)
        d = list(difflib.unified_diff(lines, "\n".join(new).split("\n"), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=1))
        diffs.extend(d)
        total += len(applied)
        ids.extend(applied)
    shown = diffs[: max(10, int(max_lines))]
    return {"project_id": project_id, "fixes": total, "fix_ids": ids, "diff": "\n".join(shown),
            "truncated": len(diffs) > len(shown), "files": len(per_file),
            "next": "Approve, then call code.fix_apply with the fix_ids you want (all of them, or a subset)."}


def fix_apply(project_id: str, fix_ids: list[str]) -> dict[str, Any]:
    from .snapstore import create_snapshot

    if not fix_ids:
        raise ValueError("Pass the fix_ids to apply (from code.fix_preview).")
    root, per_file = _collect_fixes(project_id, fix_ids, None, None)
    found = {fx["fix_id"] for fixes in per_file.values() for fx in fixes}
    missing = sorted(set(fix_ids) - found)
    if not per_file:
        raise ValueError(f"None of those fixes apply any more (the code changed): {missing}")
    snap = create_snapshot(paths=[str(p) for p in per_file], kind="auto", description="Before code.fix_apply")
    applied_all, stale_all = [], list(missing)
    for f, fixes in per_file.items():
        raw = f.read_bytes()
        crlf = b"\r\n" in raw
        lines = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
        new, applied, stale = _apply_to_lines(lines, fixes)
        text = "\n".join(new)
        if crlf:
            text = text.replace("\n", "\r\n")
        f.write_bytes(text.encode("utf-8"))
        applied_all += applied
        stale_all += stale
    return {"applied": sorted(applied_all), "skipped_stale": sorted(stale_all), "files_changed": len(per_file),
            "snapshot_id": snap["snapshot_id"],
            "undo": f"storage.restore with snapshot_id {snap['snapshot_id']}"}


def list_rules() -> dict[str, Any]:
    fams = {"lua": "Luau/Lua", "js": "JavaScript/TypeScript", "java": "Java", "kotlin": "Kotlin"}
    return {"rules": [{"id": r.id, "language": fams[r.family], "severity": r.severity, "message": r.message} for r in RULES]}


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
        id="code.review", area="code",
        summary="Find bugs and risky patterns in Luau, JS (incl. Hearth rules), Java and Kotlin (incl. Minecraft client/server mistakes). Marks which findings have an automatic fix.",
        input_schema={"project_id": "str", "paths": "optional list[str]", "rules": "optional list[str]",
                      "min_severity": "optional str", "limit": "optional int"},
        permission="read",
        handler=wrap(lambda a: review(a["project_id"], a.get("paths"), a.get("rules"), a.get("min_severity", "info"), int(a.get("limit", 100))), "Review done."),
        tags=["code", "review", "lint", "bugs", "luau", "javascript", "java", "kotlin", "minecraft", "roblox"],
    ))
    reg.register(CapabilitySpec(
        id="code.rules", area="code", summary="List the code review rules by language and severity.",
        input_schema={}, permission="read", handler=wrap(lambda a: list_rules(), "Rules listed."), tags=["code", "review"],
    ))
    reg.register(CapabilitySpec(
        id="code.fix_preview", area="code",
        summary="Show the exact unified diff for automatic fixes (all, by rule, by file, or by fix_id). Changes nothing.",
        input_schema={"project_id": "str", "fix_ids": "optional list[str]", "paths": "optional list[str]",
                      "rules": "optional list[str]", "max_lines": "optional int"},
        permission="read",
        handler=wrap(lambda a: fix_preview(a["project_id"], a.get("fix_ids"), a.get("paths"), a.get("rules"), int(a.get("max_lines", 400))), "Fix preview ready."),
        tags=["code", "fix", "diff", "preview"],
    ))
    reg.register(CapabilitySpec(
        id="code.fix_apply", area="code",
        summary="Apply the approved fixes by fix_id. Skips any line that changed since the preview; snapshots files first so it can be undone.",
        input_schema={"project_id": "str", "fix_ids": "list[str]"},
        permission="write",
        handler=wrap(lambda a: fix_apply(a["project_id"], a["fix_ids"]), "Fixes applied."),
        tags=["code", "fix", "apply"],
    ))
