"""Language-aware source scanning for Luau/Lua, JavaScript/TypeScript, Java,
Kotlin, and Python, with no third-party parsers.

The core trick: ``mask`` replaces comment and string *contents* with spaces
while keeping every newline, so later regexes never match inside a comment or
a string and every match still has its true line number.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterable

LANG_FAMILY = {
    "lua": "lua", "luau": "lua",
    "javascript": "js", "typescript": "js",
    "java": "java", "kotlin": "kotlin",
    "python": "python",
}

KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "function", "elseif", "then", "do", "end", "local",
    "new", "typeof", "instanceof", "await", "yield", "throw", "delete", "void", "in", "of", "and", "or", "not",
    "super", "this", "class", "else", "try", "finally", "case", "default", "import", "export", "from", "as",
    "fun", "val", "var", "when", "is", "synchronized", "assert", "print", "require",
}


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------

def _blank(segment: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in segment)


def mask(text: str, family: str) -> str:
    """Blank out comments and string contents, preserving length and newlines.
    String delimiters are kept so `require("x")` still looks like a call."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if family == "lua":
            if two == "--":
                m = re.match(r"--\[(=*)\[", text[i:])
                if m:
                    close = "]" + m.group(1) + "]"
                    j = text.find(close, i + len(m.group(0)))
                    j = n if j == -1 else j + len(close)
                else:
                    j = text.find("\n", i)
                    j = n if j == -1 else j
                out.append(_blank(text[i:j]))
                i = j
                continue
            m = re.match(r"\[(=*)\[", text[i:])
            if m:
                close = "]" + m.group(1) + "]"
                j = text.find(close, i + len(m.group(0)))
                j = n if j == -1 else j + len(close)
                out.append('"' + _blank(text[i + 1:j - 1]) + '"' if j - i >= 2 else _blank(text[i:j]))
                i = j
                continue
            quotes = "\"'`"
        elif family in {"js", "java", "kotlin"}:
            if two == "//":
                j = text.find("\n", i)
                j = n if j == -1 else j
                out.append(_blank(text[i:j]))
                i = j
                continue
            if two == "/*":
                j = text.find("*/", i + 2)
                j = n if j == -1 else j + 2
                out.append(_blank(text[i:j]))
                i = j
                continue
            if family in {"java", "kotlin"} and text.startswith('"""', i):
                j = text.find('"""', i + 3)
                j = n if j == -1 else j + 3
                out.append('"' + _blank(text[i + 1:j - 1]) + '"')
                i = j
                continue
            quotes = "\"'`" if family == "js" else "\"'"
        elif family == "python":
            if c == "#":
                j = text.find("\n", i)
                j = n if j == -1 else j
                out.append(_blank(text[i:j]))
                i = j
                continue
            if text.startswith('"""', i) or text.startswith("'''", i):
                q = text[i:i + 3]
                j = text.find(q, i + 3)
                j = n if j == -1 else j + 3
                out.append('"' + _blank(text[i + 1:j - 1]) + '"')
                i = j
                continue
            quotes = "\"'"
        else:
            quotes = "\"'"
        if c in quotes:
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    break
                if text[j] == "\n" and c != "`":
                    break  # unterminated single-line string
                j += 1
            end = min(j + 1, n)
            out.append(c + _blank(text[i + 1:j]) + (text[j] if j < n else ""))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


def string_literals(text: str, family: str) -> list[tuple[int, str]]:
    """(line, value) of simple quoted strings, used for require/import paths."""
    masked = mask(text, family)
    found = []
    for m in re.finditer(r"(\"([^\"\\\n]|\\.)*\"|'([^'\\\n]|\\.)*')", text):
        if masked[m.start()] in "\"'":
            found.append((text.count("\n", 0, m.start()) + 1, m.group(0)[1:-1]))
    return found


# --------------------------------------------------------------------------
# Definitions, calls, bodies
# --------------------------------------------------------------------------

@dataclass
class Definition:
    name: str
    kind: str
    line_start: int
    line_end: int
    exported: bool = False
    complexity: int = 1
    params: int = 0
    qualifier: str = ""


@dataclass
class ScanResult:
    family: str
    definitions: list[Definition] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


LUA_DEF_PATTERNS = [
    (re.compile(r"^[ \t]*local[ \t]+function[ \t]+([A-Za-z_]\w*)[ \t]*\(([^)]*)\)", re.M), "function", False),
    (re.compile(r"^[ \t]*function[ \t]+([A-Za-z_][\w.]*[.:])?([A-Za-z_]\w*)[ \t]*\(([^)]*)\)", re.M), "function", True),
    (re.compile(r"^[ \t]*(local[ \t]+)?(?:([A-Za-z_][\w.]*)\.)?([A-Za-z_]\w*)[ \t]*=[ \t]*function[ \t]*\(([^)]*)\)", re.M), "function", None),
]
LUA_OPEN = re.compile(r"\b(function|then|do|repeat)\b")
LUA_CLOSE = re.compile(r"\b(end|until)\b")
LUA_DECISIONS = re.compile(r"\b(if|elseif|while|for|repeat|and|or)\b")

JS_DEF_PATTERNS = [
    (re.compile(r"^[ \t]*(export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function\*?[ \t]+([A-Za-z_$][\w$]*)[ \t]*\(([^)]*)\)", re.M), "function"),
    (re.compile(r"^[ \t]*(export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*(?:async[ \t]+)?(?:function\b[^(]*\(([^)]*)\)|\(([^)]*)\)[ \t]*=>|([A-Za-z_$][\w$]*)[ \t]*=>)", re.M), "function"),
    (re.compile(r"^[ \t]*(export[ \t]+)?(?:default[ \t]+)?class[ \t]+([A-Za-z_$][\w$]*)", re.M), "class"),
    (re.compile(r"^[ \t]*()(?:static[ \t]+)?(?:async[ \t]+)?(?:get[ \t]+|set[ \t]+)?([A-Za-z_$][\w$]*)[ \t]*\(([^)]*)\)[ \t]*\{", re.M), "method"),
]
JS_DECISIONS = re.compile(r"\b(if|for|while|case|catch)\b|&&|\|\||\?\?|\?(?!\.)")

JAVA_TYPE = re.compile(r"^[ \t]*(?:@\w+(?:\([^)]*\))?\s+)*((?:(?:public|protected|private|abstract|static|final|sealed|non-sealed)\s+)*)(class|interface|enum|record)\s+([A-Za-z_]\w*)", re.M)
JAVA_METHOD = re.compile(
    r"^[ \t]*((?:(?:public|protected|private|abstract|static|final|synchronized|native|default)\s+)*)"
    r"(?:<[^>]+>\s+)?[\w.<>\[\],? ]+?\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(?:throws\s+[\w.,\s]+)?\{", re.M)
KOTLIN_FUN = re.compile(r"^[ \t]*((?:(?:public|private|protected|internal|override|open|suspend|inline|operator)\s+)*)fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\(([^)]*)\)", re.M)
KOTLIN_TYPE = re.compile(r"^[ \t]*((?:(?:public|private|protected|internal|data|sealed|abstract|open|enum|inner)\s+)*)(class|interface|object)\s+([A-Za-z_]\w*)", re.M)
JAVA_DECISIONS = re.compile(r"\b(if|for|while|case|catch|when)\b|&&|\|\||\?(?!\.)|\?:")

CALL_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
LUA_METHOD_CALL = re.compile(r"[.:]([A-Za-z_]\w*)\s*\(")


def _count_params(raw: str) -> int:
    raw = raw.strip()
    return 0 if not raw else raw.count(",") + 1


def _brace_end(masked: str, start: int) -> int:
    """Index just past the block that opens at the first '{' at or after start."""
    i = masked.find("{", start)
    if i == -1:
        return start
    depth = 0
    for j in range(i, len(masked)):
        ch = masked[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(masked)


def _lua_end(masked: str, start: int) -> int:
    """Index just past the `end` that closes a function starting at start."""
    depth = 0
    pos = start
    tokens = re.finditer(r"\b(function|then|do|repeat|end|until|elseif|while|for)\b", masked[start:])
    pending_loop = 0
    for m in tokens:
        tok = m.group(1)
        if tok in {"while", "for"}:
            pending_loop += 1  # their `do` opens the block; counted at `do`
            continue
        if tok == "elseif":
            depth -= 1  # `elseif ... then` re-opens; balance the coming `then`
            continue
        if tok in {"function", "then", "do", "repeat"}:
            if tok == "do" and pending_loop:
                pending_loop -= 1
            depth += 1
        elif tok in {"end", "until"}:
            depth -= 1
            if depth == 0:
                return start + m.end()
        pos = start + m.end()
    return len(masked)


def scan(text: str, language: str) -> ScanResult:
    family = LANG_FAMILY.get(language, "")
    result = ScanResult(family=family)
    if family == "python":
        return _scan_python(text, result)
    if not family:
        return result
    masked = mask(text, family)

    if family == "lua":
        for pat, kind, exported_flag in LUA_DEF_PATTERNS:
            for m in pat.finditer(masked):
                if pat is LUA_DEF_PATTERNS[0][0]:
                    qual, name, params = "", m.group(1), m.group(2)
                    exported = False
                elif pat is LUA_DEF_PATTERNS[1][0]:
                    qual, name, params = (m.group(1) or "").rstrip(".:"), m.group(2), m.group(3)
                    exported = bool(qual)  # function M.x / M:x is the module's public API
                else:
                    qual, name, params = m.group(2) or "", m.group(3), m.group(4)
                    exported = bool(qual) and not m.group(1)
                fstart = m.start() + m.group(0).find("function")
                end = _lua_end(masked, fstart)
                body = masked[fstart:end]
                result.definitions.append(Definition(
                    name=name, kind=kind, line_start=_line_of(masked, m.start()), line_end=_line_of(masked, end),
                    exported=exported, complexity=1 + len(LUA_DECISIONS.findall(body)), params=_count_params(params),
                    qualifier=qual,
                ))
        seen = set()
        uniq = []
        for d in sorted(result.definitions, key=lambda d: (d.line_start, d.name)):
            if (d.name, d.line_start) not in seen:
                seen.add((d.name, d.line_start))
                uniq.append(d)
        result.definitions = uniq
        def_lines = {(d.name, d.line_start) for d in result.definitions}
        for m in CALL_RE.finditer(masked):
            name = m.group(1)
            line = _line_of(masked, m.start())
            if name in KEYWORDS or (name, line) in def_lines:
                continue
            before = masked[max(0, m.start() - 9):m.start()]
            if re.search(r"function\s*$", before):
                continue
            result.calls.append((name, line))
        return result

    if family == "js":
        for pat, kind in JS_DEF_PATTERNS:
            for m in pat.finditer(masked):
                name = m.group(2)
                if name in KEYWORDS:
                    continue
                params = ""
                if kind == "function":
                    params = next((g for g in m.groups()[2:] if g is not None), "") if len(m.groups()) > 2 else ""
                elif kind == "method":
                    params = m.group(3) or ""
                exported = bool(m.group(1))
                end = _brace_end(masked, m.end() - 1) if kind != "class" else _brace_end(masked, m.start())
                if end <= m.start():
                    end = m.end()
                body = masked[m.start():end]
                result.definitions.append(Definition(
                    name=name, kind=kind, line_start=_line_of(masked, m.start()), line_end=_line_of(masked, end),
                    exported=exported, complexity=1 + (len(JS_DECISIONS.findall(body)) if kind != "class" else 0),
                    params=_count_params(params),
                ))
    else:  # java / kotlin
        type_pat = JAVA_TYPE if family == "java" else KOTLIN_TYPE
        for m in type_pat.finditer(masked):
            mods = m.group(1) or ""
            end = _brace_end(masked, m.end())
            result.definitions.append(Definition(
                name=m.group(3), kind=m.group(2), line_start=_line_of(masked, m.start()),
                line_end=_line_of(masked, end), exported="private" not in mods,
            ))
        meth_pat = JAVA_METHOD if family == "java" else KOTLIN_FUN
        for m in meth_pat.finditer(masked):
            name = m.group(2)
            if name in KEYWORDS or name in {"else", "new"}:
                continue
            mods = m.group(1) or ""
            end = _brace_end(masked, m.end() - 1) if family == "java" else _brace_end(masked, m.end())
            if family == "kotlin" and masked.find("{", m.end()) > masked.find("\n", m.end()) >= 0 and "=" in masked[m.end():masked.find("\n", m.end())]:
                end = masked.find("\n", m.end())  # expression body: fun x() = ...
            body = masked[m.start():max(end, m.end())]
            result.definitions.append(Definition(
                name=name, kind="method" if family == "java" else "function",
                line_start=_line_of(masked, m.start()), line_end=_line_of(masked, max(end, m.end())),
                exported=("private" not in mods), complexity=1 + len(JAVA_DECISIONS.findall(body)),
                params=_count_params(m.group(3) or ""),
            ))
    result.definitions.sort(key=lambda d: (d.line_start, d.name))
    def_lines = {(d.name, d.line_start) for d in result.definitions}
    for m in CALL_RE.finditer(masked):
        name = m.group(1)
        line = _line_of(masked, m.start())
        if name in KEYWORDS or (name, line) in def_lines:
            continue
        result.calls.append((name, line))
    return result


def _scan_python(text: str, result: ScanResult) -> ScanResult:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return result
    decision_nodes = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.IfExp, ast.ExceptHandler,
                      ast.comprehension, ast.AsyncFor, ast.AsyncWith)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            complexity = 1 + sum(1 for sub in ast.walk(node) if isinstance(sub, decision_nodes))
            params = len(node.args.args) if not isinstance(node, ast.ClassDef) else 0
            result.definitions.append(Definition(
                name=node.name, kind="class" if isinstance(node, ast.ClassDef) else "function",
                line_start=node.lineno, line_end=getattr(node, "end_lineno", node.lineno),
                exported=not node.name.startswith("_"), complexity=complexity if not isinstance(node, ast.ClassDef) else 1,
                params=params,
            ))
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name:
                result.calls.append((name, node.lineno))
    result.definitions.sort(key=lambda d: (d.line_start, d.name))
    return result


# --------------------------------------------------------------------------
# Duplicate detection helpers
# --------------------------------------------------------------------------

def normalized_lines(text: str, family: str) -> list[tuple[int, str]]:
    """Meaningful code lines (comments/strings blanked, whitespace collapsed),
    skipping blank and bracket-only lines."""
    masked = mask(text, family) if family else text
    out = []
    for i, line in enumerate(masked.splitlines(), start=1):
        norm = re.sub(r"\s+", " ", line).strip()
        if len(norm) < 4 or re.fullmatch(r"[{}()\[\];,]+|end|end\)|else|do|then", norm):
            continue
        out.append((i, norm))
    return out


def windows(lines: list[tuple[int, str]], size: int) -> Iterable[tuple[int, int, str]]:
    for k in range(0, max(0, len(lines) - size + 1)):
        chunk = lines[k:k + size]
        yield chunk[0][0], chunk[-1][0], "\n".join(t for _, t in chunk)
