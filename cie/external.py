"""Wrappers around external tools: git, linters/type checkers, security scanners, dependency audits,
headless game engine, Aseprite, ffmpeg, sox, loudness, atlas packing, and local semantic search.

Rules for every wrapper:
- Executables are looked up with shutil.which and must be on a fixed allowlist.
- Arguments are built from validated templates and run with shell=False.
- Paths go through resolve_in_projects, so they stay inside approved project folders.
- A missing tool returns status "tool_missing" with install guidance, never a crash.
- Output is capped so one tool call cannot flood the conversation.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import get_cie_home, load_config, resolve_in_projects

MAX_OUTPUT_CHARS = 20_000
DEFAULT_TIMEOUT = 120

INSTALL_HINTS = {
    "git": "Install Git for Windows from git-scm.com.",
    "ruff": "pip install ruff",
    "pyright": "pip install pyright",
    "mypy": "pip install mypy",
    "bandit": "pip install bandit",
    "semgrep": "pip install semgrep (needs network on first rule fetch)",
    "gitleaks": "Download the gitleaks release for Windows from github.com/gitleaks/gitleaks and put gitleaks.exe on PATH.",
    "pip-audit": "pip install pip-audit",
    "pip-licenses": "pip install pip-licenses",
    "godot": "Install Godot 4 and add its folder to PATH (the console build works best, e.g. Godot_v4.x_win64_console.exe renamed to godot.exe).",
    "aseprite": "Install Aseprite (paid, or build from source) and add it to PATH.",
    "ffmpeg": "Download an ffmpeg build for Windows from ffmpeg.org and add its bin folder to PATH.",
    "sox": "Install SoX from sourceforge.net/projects/sox and add it to PATH.",
}

ALLOWED_TOOLS = set(INSTALL_HINTS)


class ToolMissing(Exception):
    pass


def find_tool(name: str) -> str:
    if name not in ALLOWED_TOOLS:
        raise PermissionError(f"Tool '{name}' is not on the allowlist.")
    path = shutil.which(name)
    if not path:
        raise ToolMissing(name)
    return path


def _missing(name: str, capability: str) -> dict[str, Any]:
    return {
        "status": "tool_missing",
        "capability": capability,
        "summary": f"'{name}' is not installed or not on PATH. {INSTALL_HINTS.get(name, '')}".strip(),
    }


def _run(argv: list[str], timeout: int = DEFAULT_TIMEOUT, cwd: Path | None = None) -> dict[str, Any]:
    """Run a fixed argv list without a shell and return truncated output."""
    try:
        proc = subprocess.run(
            argv,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(int(timeout), 600)),
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return {"returncode": None, "stdout": "", "stderr": f"Timed out after {timeout}s", "timed_out": True}
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[:MAX_OUTPUT_CHARS],
        "stderr": proc.stderr[:MAX_OUTPUT_CHARS],
        "timed_out": False,
    }


def _project_root_for(path: Path, entry: dict[str, Any]) -> Path:
    return Path(entry["path"]).resolve()


def _safe_out(raw: str) -> Path:
    """Output files must be inside an approved project. resolve_in_projects allows new files inside a root."""
    resolved, _ = resolve_in_projects(raw)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


# ---------- Git ----------

def git_log(path: str = "", count: int = 20) -> dict[str, Any]:
    if not 1 <= int(count) <= 200:
        raise ValueError("count must be between 1 and 200.")
    try:
        git = find_tool("git")
    except ToolMissing:
        return _missing("git", "git.log")
    target, entry = resolve_in_projects(path or ".")
    root = _project_root_for(target, entry)
    argv = [git, "--no-pager", "log", f"-n{int(count)}", "--oneline", "--no-color"]
    argv += ["--", str(target.relative_to(root))] if path else []
    res = _run(argv, cwd=root)
    return {"status": "ok" if res["returncode"] == 0 else "error", "summary": "git log", "data": res}


def git_diff(path: str = "", against: str = "HEAD") -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._/~^-]{1,80}", against) or against.startswith("-"):
        raise ValueError("Invalid revision.")
    try:
        git = find_tool("git")
    except ToolMissing:
        return _missing("git", "git.diff")
    target, entry = resolve_in_projects(path or ".")
    root = _project_root_for(target, entry)
    argv = [git, "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--no-color", against]
    argv += ["--", str(target.relative_to(root))] if path else []
    res = _run(argv, cwd=root)
    return {"status": "ok" if res["returncode"] == 0 else "error", "summary": "git diff", "data": res}


def git_blame(path: str, start_line: int = 1, end_line: int = 40) -> dict[str, Any]:
    try:
        git = find_tool("git")
    except ToolMissing:
        return _missing("git", "git.blame")
    target, entry = resolve_in_projects(path)
    root = _project_root_for(target, entry)
    s, e = int(start_line), int(end_line)
    if s < 1 or e < s or e - s > 500:
        raise ValueError("Invalid line range (max 500 lines).")
    argv = [git, "--no-pager", "blame", "--no-color", "-L", f"{s},{e}", "--", str(target.relative_to(root))]
    res = _run(argv, cwd=root)
    return {"status": "ok" if res["returncode"] == 0 else "error", "summary": "git blame", "data": res}


# ---------- Linters and type checkers ----------

def lint_ruff(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("ruff")
    except ToolMissing:
        return _missing("ruff", "lint.ruff")
    target, _ = resolve_in_projects(path or ".")
    res = _run([exe, "check", "--output-format", "json", "--no-cache", str(target)])
    try:
        findings = json.loads(res["stdout"] or "[]")
    except json.JSONDecodeError:
        return {"status": "error", "summary": "ruff output was not JSON", "data": res}
    trimmed = [
        {"file": f.get("filename"), "line": (f.get("location") or {}).get("row"),
         "code": f.get("code"), "message": f.get("message")}
        for f in findings[:100]
    ]
    return {"status": "ok", "summary": f"ruff: {len(findings)} finding(s)", "data": {"findings": trimmed, "total": len(findings)}}


def lint_pyright(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("pyright")
    except ToolMissing:
        return _missing("pyright", "lint.pyright")
    target, _ = resolve_in_projects(path or ".")
    res = _run([exe, "--outputjson", str(target)], timeout=300)
    try:
        report = json.loads(res["stdout"] or "{}")
    except json.JSONDecodeError:
        return {"status": "error", "summary": "pyright output was not JSON", "data": res}
    diags = report.get("generalDiagnostics", [])
    errors = [d for d in diags if d.get("severity") == "error"]
    return {
        "status": "ok",
        "summary": f"pyright: {len(errors)} error(s), {len(diags)} diagnostic(s) total",
        "data": {"errors": errors[:100], "total": len(diags)},
    }


def lint_mypy(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("mypy")
    except ToolMissing:
        return _missing("mypy", "lint.mypy")
    target, _ = resolve_in_projects(path or ".")
    res = _run([exe, "--no-incremental", "--no-error-summary", str(target)], timeout=300)
    lines = [ln for ln in res["stdout"].splitlines() if ln.strip()]
    return {"status": "ok", "summary": f"mypy: {len(lines)} line(s) of output", "data": {"lines": lines[:200]}}


# ---------- Security scanners ----------

def security_bandit(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("bandit")
    except ToolMissing:
        return _missing("bandit", "security.bandit")
    target, _ = resolve_in_projects(path or ".")
    res = _run([exe, "-q", "-r", "-f", "json", str(target)], timeout=300)
    try:
        report = json.loads(res["stdout"] or "{}")
    except json.JSONDecodeError:
        return {"status": "error", "summary": "bandit output was not JSON", "data": res}
    results = report.get("results", [])
    return {
        "status": "ok",
        "summary": f"bandit: {len(results)} issue(s)",
        "data": {"issues": [
            {"file": r.get("filename"), "line": r.get("line_number"), "test": r.get("test_id"),
             "severity": r.get("issue_severity"), "message": r.get("issue_text")}
            for r in results[:100]
        ]},
    }


def security_gitleaks(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("gitleaks")
    except ToolMissing:
        return _missing("gitleaks", "security.gitleaks")
    target, _ = resolve_in_projects(path or ".")
    # Reports can contain matched secrets. Keep them in a private temporary file and
    # remove it even when gitleaks fails or JSON parsing raises an exception.
    report_path: Path | None = None
    findings: list[Any] = []
    try:
        with tempfile.NamedTemporaryFile(prefix="cie-gitleaks-", suffix=".json", dir=str(get_cie_home()), delete=False) as tmp:
            report_path = Path(tmp.name)
        result = _run([exe, "dir", str(target), "--no-banner", "-f", "json", "-r", str(report_path)], timeout=300)
        if result.get("returncode") not in (0, 1):
            return {"status": "error", "summary": "gitleaks execution failed", "data": {"returncode": result.get("returncode"), "timed_out": result.get("timed_out", False)}}
        if report_path.exists():
            try:
                findings = json.loads(report_path.read_text(encoding="utf-8") or "[]")
            except json.JSONDecodeError:
                return {"status": "error", "summary": "gitleaks returned invalid JSON", "data": {}}
        # Never return the matched secret text, only where it is.
        safe = [{"file": f.get("File"), "line": f.get("StartLine"), "rule": f.get("RuleID")} for f in findings[:100]]
        return {"status": "ok", "summary": f"gitleaks: {len(findings)} possible secret(s)", "data": {"findings": safe}}
    finally:
        if report_path is not None:
            try:
                report_path.unlink(missing_ok=True)
            except OSError:
                pass


def security_semgrep(path: str = "") -> dict[str, Any]:
    try:
        exe = find_tool("semgrep")
    except ToolMissing:
        return _missing("semgrep", "security.semgrep")
    target, _ = resolve_in_projects(path or ".")
    res = _run([exe, "scan", "--config", "p/default", "--json", "--quiet", str(target)], timeout=600)
    try:
        results = json.loads(res["stdout"] or "{}").get("results", [])
    except json.JSONDecodeError:
        return {"status": "error", "summary": "semgrep output was not JSON", "data": res}
    return {
        "status": "ok",
        "summary": f"semgrep: {len(results)} finding(s). Note: p/default rules are fetched from the network.",
        "data": {"findings": [
            {"file": r.get("path"), "line": (r.get("start") or {}).get("line"),
             "rule": r.get("check_id"), "message": (r.get("extra") or {}).get("message")}
            for r in results[:100]
        ]},
    }


# ---------- Dependencies ----------

def deps_audit(requirements_path: str = "requirements.txt") -> dict[str, Any]:
    try:
        exe = find_tool("pip-audit")
    except ToolMissing:
        return _missing("pip-audit", "deps.audit")
    target, _ = resolve_in_projects(requirements_path)
    res = _run([exe, "-r", str(target), "-f", "json", "--progress-spinner", "off"], timeout=300)
    try:
        deps = json.loads(res["stdout"] or "{}").get("dependencies", [])
    except json.JSONDecodeError:
        return {"status": "error", "summary": "pip-audit output was not JSON", "data": res}
    vulnerable = [{"package": d.get("name"), "version": d.get("version"),
                   "vulns": [v.get("id") for v in d.get("vulns", [])]}
                  for d in deps if d.get("vulns")]
    return {"status": "ok", "summary": f"pip-audit: {len(vulnerable)} vulnerable package(s)", "data": {"vulnerable": vulnerable}}


def deps_licenses() -> dict[str, Any]:
    try:
        exe = find_tool("pip-licenses")
    except ToolMissing:
        return _missing("pip-licenses", "deps.licenses")
    res = _run([exe, "--format=json"], timeout=120)
    try:
        rows = json.loads(res["stdout"] or "[]")
    except json.JSONDecodeError:
        return {"status": "error", "summary": "pip-licenses output was not JSON", "data": res}
    return {"status": "ok", "summary": f"{len(rows)} package(s) with licenses",
            "data": {"packages": [{"name": r.get("Name"), "version": r.get("Version"), "license": r.get("License")} for r in rows]}}


# ---------- Game engine and art tools ----------

def engine_godot_check(project_path: str) -> dict[str, Any]:
    """Load a Godot project headless and report script errors. The project must contain project.godot."""
    try:
        exe = find_tool("godot")
    except ToolMissing:
        return _missing("godot", "engine.godot_check")
    target, _ = resolve_in_projects(project_path)
    if target.is_file():
        target = target.parent
    if not (target / "project.godot").exists():
        raise ValueError("No project.godot found in that folder.")
    res = _run([exe, "--headless", "--path", str(target), "--quit"], timeout=180)
    text = (res["stdout"] + "\n" + res["stderr"])
    errors = [ln.strip() for ln in text.splitlines() if "ERROR" in ln or "SCRIPT ERROR" in ln]
    ok = res["returncode"] == 0 and not errors and not res["timed_out"]
    return {"status": "ok" if ok else "error",
            "summary": "Godot project loads cleanly." if ok else f"Godot reported {len(errors)} error line(s).",
            "data": {"errors": errors[:100], "returncode": res["returncode"], "timed_out": res["timed_out"]}}


def art_aseprite_export(source: str, sheet_png: str, sheet_json: str) -> dict[str, Any]:
    """Export an .aseprite file to a sprite sheet PNG plus JSON (frames and tags) inside a project."""
    try:
        exe = find_tool("aseprite")
    except ToolMissing:
        return _missing("aseprite", "art.aseprite_export")
    src, _ = resolve_in_projects(source)
    if src.suffix.lower() not in {".aseprite", ".ase"}:
        raise ValueError("Source must be .aseprite or .ase.")
    out_png = _safe_out(sheet_png)
    out_json = _safe_out(sheet_json)
    if out_png.suffix.lower() != ".png" or out_json.suffix.lower() != ".json":
        raise ValueError("Outputs must be .png and .json.")
    res = _run([exe, "-b", str(src), "--sheet", str(out_png), "--data", str(out_json),
                "--format", "json-array", "--list-tags"], timeout=180)
    ok = res["returncode"] == 0 and out_png.exists()
    return {"status": "ok" if ok else "error", "summary": "Aseprite export done." if ok else "Aseprite export failed.",
            "artifacts": [str(out_png), str(out_json)] if ok else [], "data": res}


def art_atlas_pack(paths: list[str], out_png: str, out_json: str, padding: int = 2, max_width: int = 2048) -> dict[str, Any]:
    """Pack PNG sprites into one atlas with a simple shelf packer (pure Pillow, no external tool needed)."""
    from PIL import Image

    if not paths or len(paths) > 1000:
        raise ValueError("Provide between 1 and 1000 PNG paths.")
    pad = max(0, min(int(padding), 32))
    width_limit = max(64, min(int(max_width), 8192))
    images = []
    for raw in paths:
        src, _ = resolve_in_projects(raw)
        if src.suffix.lower() != ".png":
            raise ValueError(f"Only PNG sprites can be packed: {raw}")
        img = Image.open(src).convert("RGBA")
        images.append((src.name, img))
    images.sort(key=lambda t: t[1].height, reverse=True)
    placements: list[dict[str, Any]] = []
    x = y = shelf_h = 0
    atlas_w = 0
    for name, img in images:
        w, h = img.width + pad, img.height + pad
        if w > width_limit:
            raise ValueError(f"Sprite '{name}' is wider than max_width.")
        if x + w > width_limit:
            x, y, shelf_h = 0, y + shelf_h, 0
        placements.append({"name": name, "x": x, "y": y, "w": img.width, "h": img.height})
        x += w
        shelf_h = max(shelf_h, h)
        atlas_w = max(atlas_w, x)
    atlas_h = y + shelf_h
    atlas = Image.new("RGBA", (max(atlas_w, 1), max(atlas_h, 1)), (0, 0, 0, 0))
    for (name, img), place in zip(sorted(images, key=lambda t: t[1].height, reverse=True), placements):
        atlas.paste(img, (place["x"], place["y"]))
    png = _safe_out(out_png)
    js = _safe_out(out_json)
    atlas.save(png)
    js.write_text(json.dumps({"size": [atlas.width, atlas.height], "frames": placements}, indent=2), encoding="utf-8")
    return {"status": "ok", "summary": f"Packed {len(placements)} sprite(s) into {atlas.width}x{atlas.height}.",
            "artifacts": [str(png), str(js)], "data": {"frames": len(placements), "size": [atlas.width, atlas.height]}}


# ---------- Audio ----------

def audio_ffmpeg_convert(source: str, output: str) -> dict[str, Any]:
    try:
        exe = find_tool("ffmpeg")
    except ToolMissing:
        return _missing("ffmpeg", "audio.ffmpeg_convert")
    src, _ = resolve_in_projects(source)
    out = _safe_out(output)
    if out.suffix.lower() not in {".wav", ".ogg", ".mp3", ".flac"}:
        raise ValueError("Output must be .wav, .ogg, .mp3, or .flac.")
    res = _run([exe, "-hide_banner", "-n", "-i", str(src), str(out)], timeout=180)
    ok = res["returncode"] == 0 and out.exists()
    return {"status": "ok" if ok else "error", "summary": "Converted." if ok else "ffmpeg failed.",
            "artifacts": [str(out)] if ok else [], "data": {"stderr_tail": res["stderr"][-2000:]}}


SOX_EFFECTS = {"trim", "reverb", "pitch", "gain", "norm", "fade", "rate", "lowpass", "highpass"}


def audio_sox_effect(source: str, output: str, effect: str, args: list[str] | None = None) -> dict[str, Any]:
    # Validate everything before looking for the tool, so bad input is always rejected the same way.
    if effect not in SOX_EFFECTS:
        raise ValueError(f"Effect must be one of: {', '.join(sorted(SOX_EFFECTS))}.")
    clean_args = [str(a) for a in (args or [])]
    if len(clean_args) > 8 or any(not re.fullmatch(r"-?[0-9.]{1,12}", a) for a in clean_args):
        raise ValueError("Effect arguments must be short numbers.")
    src, _ = resolve_in_projects(source)
    out = _safe_out(output)
    if out.suffix.lower() != ".wav":
        raise ValueError("Output must be .wav.")
    try:
        exe = find_tool("sox")
    except ToolMissing:
        return _missing("sox", "audio.sox_effect")
    res = _run([exe, str(src), str(out), effect, *clean_args], timeout=180)
    ok = res["returncode"] == 0 and out.exists()
    return {"status": "ok" if ok else "error", "summary": "Effect applied." if ok else "sox failed.",
            "artifacts": [str(out)] if ok else [], "data": {"stderr_tail": res["stderr"][-2000:]}}


def audio_lufs(path: str) -> dict[str, Any]:
    """Integrated loudness (LUFS, ITU-R BS.1770) and true peak. Pure Python, no external tool."""
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf

    src, _ = resolve_in_projects(path)
    data, rate = sf.read(str(src), always_2d=True)
    if data.shape[0] < rate * 0.4:
        raise ValueError("Clip is shorter than 0.4 s; LUFS needs at least 400 ms.")
    meter = pyln.Meter(rate)
    loudness = float(meter.integrated_loudness(data))
    peak = float(np.max(np.abs(data)))
    peak_dbfs = 20 * np.log10(peak) if peak > 0 else float("-inf")
    return {"status": "ok", "summary": f"{loudness:.1f} LUFS integrated, peak {peak_dbfs:.1f} dBFS",
            "data": {"integrated_lufs": round(loudness, 2), "peak_dbfs": round(float(peak_dbfs), 2), "sample_rate": rate}}


# ---------- Local semantic search ----------

SEMANTIC_EXTS = {".py", ".gd", ".cs", ".js", ".ts", ".md", ".txt", ".json", ".gdshader", ".tscn", ".tres"}
CHUNK_LINES = 40
MAX_FILE_BYTES = 300_000


def _semantic_model_dir() -> Path | None:
    raw = (load_config().get("semantic_model_dir") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _load_embedder():
    model_dir = _semantic_model_dir()
    if model_dir is None:
        return None
    from sentence_transformers import SentenceTransformer  # optional dependency
    return SentenceTransformer(str(model_dir), local_files_only=True)


def search_semantic_index(project_id: str = "") -> dict[str, Any]:
    """Build a local embedding index of project text. Requires semantic_model_dir in config (never auto-downloads)."""
    from .config import iter_project_files, project_roots

    model = _load_embedder()
    if model is None:
        return {"status": "model_missing",
                "summary": "No local embedding model configured. Download a sentence-transformers model folder "
                           "yourself, then set semantic_model_dir in the CIE config. CIE never downloads models."}
    import numpy as np

    chunks: list[dict[str, Any]] = []
    for r in project_roots():
        if project_id and r["id"] != project_id:
            continue
        for f in iter_project_files(r, extensions=SEMANTIC_EXTS):
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            for start in range(0, len(lines), CHUNK_LINES):
                text = "\n".join(lines[start:start + CHUNK_LINES]).strip()
                if text:
                    chunks.append({"file": str(f), "start_line": start + 1, "text": text[:2000]})
    if not chunks:
        return {"status": "ok", "summary": "Nothing to index.", "data": {"chunks": 0}}
    vectors = model.encode([c["text"] for c in chunks], normalize_embeddings=True)
    index_path = get_cie_home() / "semantic_index.json"
    payload = {"chunks": [{"file": c["file"], "start_line": c["start_line"], "text": c["text"][:300]} for c in chunks],
               "vectors": np.asarray(vectors).round(5).tolist()}
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    return {"status": "ok", "summary": f"Indexed {len(chunks)} chunk(s).", "data": {"chunks": len(chunks)}}


def search_semantic_query(query: str, limit: int = 5) -> dict[str, Any]:
    model = _load_embedder()
    if model is None:
        return {"status": "model_missing", "summary": "No local embedding model configured (see search.semantic_index)."}
    index_path = get_cie_home() / "semantic_index.json"
    if not index_path.exists():
        return {"status": "error", "summary": "No semantic index yet. Run search.semantic_index first."}
    import numpy as np

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    vectors = np.asarray(payload["vectors"], dtype=np.float32)
    q = np.asarray(model.encode([query], normalize_embeddings=True), dtype=np.float32)[0]
    scores = vectors @ q
    top = np.argsort(-scores)[: max(1, min(int(limit), 20))]
    hits = [{"file": payload["chunks"][i]["file"], "line": payload["chunks"][i]["start_line"],
             "score": round(float(scores[i]), 3), "snippet": payload["chunks"][i]["text"][:300]} for i in top]
    return {"status": "ok", "summary": f"{len(hits)} semantic hit(s).", "data": {"hits": hits}}


# ---------- Tool availability report ----------

def tools_report() -> dict[str, Any]:
    installed = {name: shutil.which(name) is not None for name in sorted(ALLOWED_TOOLS)}
    missing = [n for n, ok in installed.items() if not ok]
    return {"status": "ok",
            "summary": f"{len(installed) - len(missing)} of {len(installed)} external tools available.",
            "data": {"installed": installed, "install_hints": {n: INSTALL_HINTS[n] for n in missing}}}


# ---------- Registration ----------

def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def spec(cid: str, area: str, summary: str, perm: str, fn, schema: dict[str, Any], tags: list[str] | None = None):
        reg.register(CapabilitySpec(
            id=cid, area=area, summary=summary, input_schema=schema, permission=perm,
            handler=lambda args, _fn=fn: _fn(**{k: v for k, v in args.items()}),
            tags=tags or [area],
        ))

    spec("git.log", "git", "Recent commits, optionally for one file or folder.", "read", git_log,
         {"path": "optional str", "count": "optional int 1-200"}, ["git", "history"])
    spec("git.diff", "git", "Compact git diff against a revision, optionally for one file.", "read", git_diff,
         {"path": "optional str", "against": "optional str"}, ["git", "diff", "changes"])
    spec("git.blame", "git", "Who last changed each line in a range.", "read", git_blame,
         {"path": "str", "start_line": "int", "end_line": "int"}, ["git", "blame"])
    spec("lint.ruff", "lint", "Fast Python lint with ruff (JSON findings).", "read", lint_ruff,
         {"path": "optional str"}, ["lint", "python"])
    spec("lint.pyright", "lint", "Python type checking with pyright.", "read", lint_pyright,
         {"path": "optional str"}, ["lint", "types", "python"])
    spec("lint.mypy", "lint", "Python type checking with mypy.", "read", lint_mypy,
         {"path": "optional str"}, ["lint", "types", "python"])
    spec("security.bandit", "security", "Python security scan with bandit.", "read", security_bandit,
         {"path": "optional str"}, ["security", "python"])
    spec("security.gitleaks", "security", "Find committed secrets with gitleaks (reports locations only).", "read", security_gitleaks,
         {"path": "optional str"}, ["security", "secrets"])
    spec("security.semgrep", "security", "Multi-language security scan with semgrep (network for rules).", "read", security_semgrep,
         {"path": "optional str"}, ["security"])
    spec("deps.audit", "deps", "Check requirements.txt for known vulnerabilities (pip-audit, uses network).", "read", deps_audit,
         {"requirements_path": "optional str"}, ["deps", "security"])
    spec("deps.licenses", "deps", "List installed package licenses (pip-licenses).", "read", deps_licenses,
         {}, ["deps", "license"])
    spec("engine.godot_check", "engine", "Load a Godot project headless and report script errors.", "exec", engine_godot_check,
         {"project_path": "str"}, ["godot", "game", "engine"])
    spec("art.aseprite_export", "art", "Export an .aseprite file to a sprite sheet PNG + JSON with tags.", "write", art_aseprite_export,
         {"source": "str", "sheet_png": "str", "sheet_json": "str"}, ["art", "pixel", "sprite"])
    spec("art.atlas_pack", "art", "Pack PNG sprites into one atlas with frame coordinates JSON (pure Pillow).", "write", art_atlas_pack,
         {"paths": "list[str]", "out_png": "str", "out_json": "str", "padding": "optional int", "max_width": "optional int"},
         ["art", "atlas", "texture"])
    spec("audio.ffmpeg_convert", "audio", "Convert audio between wav, ogg, mp3, flac (never overwrites).", "write", audio_ffmpeg_convert,
         {"source": "str", "output": "str"}, ["audio", "convert"])
    spec("audio.sox_effect", "audio", "Apply one SoX effect (trim, reverb, pitch, gain, norm, fade, rate, lowpass, highpass).", "write", audio_sox_effect,
         {"source": "str", "output": "str", "effect": "str", "args": "optional list[str] of numbers"}, ["audio", "sfx", "effects"])
    spec("audio.lufs", "audio", "Integrated loudness in LUFS and peak in dBFS for a WAV file.", "read", audio_lufs,
         {"path": "str"}, ["audio", "loudness", "mix"])
    spec("search.semantic_index", "search", "Build a local meaning-based index of project text (needs a local model).", "write", search_semantic_index,
         {"project_id": "optional str"}, ["search", "semantic", "embeddings"])
    spec("search.semantic_query", "search", "Find project text by meaning using the local index.", "read", search_semantic_query,
         {"query": "str", "limit": "optional int"}, ["search", "semantic"])
    spec("core.tools_available", "core", "Which external tools are installed, and how to install the missing ones.", "read", tools_report,
         {}, ["tools", "setup"])