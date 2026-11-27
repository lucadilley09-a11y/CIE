"""Browser-game project checks and packaging (Phase 4.3, used by workflows).

  game.check    Hearth integration (hearth-connect.js first in <head>, guarded Hearth.unlock,
                localStorage-only saves, achievements listed in the entry), missing or wrongly-cased
                asset paths (GitHub Pages is case-sensitive even though Windows isn't), oversized
                files, leftover debug code
  game.package  zip a game folder into the studio (builds/) with a file manifest
"""

from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path
from typing import Any

from .config import resolve_in_projects

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".cie", "build", "dist"}
ASSET_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#?]+)["']|["'`]((?:\./)?(?:assets|sprites|audio|sounds|images|img|textures|models)/[^"'`?#]+)["'`]""")
BIG = {".png": 5_000_000, ".jpg": 5_000_000, ".webp": 5_000_000, ".gif": 5_000_000, ".wav": 10_000_000, ".ogg": 8_000_000,
       ".mp3": 8_000_000, ".glb": 20_000_000, ".gltf": 20_000_000}


def _game_dir(path: str) -> Path:
    d, _ = resolve_in_projects(path)
    if d.is_file():
        d = d.parent
    if not (d / "index.html").exists():
        raise ValueError(f"No index.html in '{d}'. Point at the game's folder.")
    return d


def _files(d: Path) -> list[Path]:
    out = []
    for p in d.rglob("*"):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(d).parts):
            out.append(p)
    return out


def check(path: str) -> dict[str, Any]:
    from .templates import _hearth_checks
    d = _game_dir(path)
    files = _files(d)
    rel = {p.relative_to(d).as_posix(): p for p in files}
    lower = {k.lower(): k for k in rel}
    issues: list[dict[str, Any]] = []
    texts = {k: p.read_bytes() for k, p in rel.items() if p.suffix.lower() in (".html", ".js", ".css", ".json")}
    hearth_files = {k: v for k, v in texts.items() if k.endswith((".html", ".js"))}
    uses_hearth = any(b"hearth" in v.lower() for v in hearth_files.values())
    if uses_hearth:
        for prob in _hearth_checks(hearth_files):
            issues.append({"severity": "error", "message": prob})
        if not any(k.endswith("hearth-entry.js") or k.endswith("games.js") for k in rel):
            issues.append({"severity": "warn", "message": "No hearth-entry.js or games.js entry found for this game."})
    missing, wrong_case = [], []
    for k, data in texts.items():
        text = data.decode("utf-8", "replace")
        base = (d / k).parent
        for m in ASSET_RE.finditer(text):
            ref = (m.group(1) or m.group(2) or "").strip()
            if not ref or ref.startswith(("http:", "https:", "data:", "//", "mailto:", "javascript:", "#", "${")) or "{" in ref:
                continue
            if "hearth-connect.js" in ref or ref.endswith("/"):
                continue
            target = (base / ref).resolve() if not ref.startswith("/") else (d / ref.lstrip("/")).resolve()
            try:
                key = target.relative_to(d.resolve()).as_posix()
            except ValueError:
                continue  # outside the game folder (e.g. ../../sdk/) - not ours to check
            if key in rel:
                continue
            if key.lower() in lower:
                wrong_case.append(f"{k}: '{ref}' (file is '{lower[key.lower()]}')")
            else:
                missing.append(f"{k}: '{ref}'")
    for msg in sorted(set(missing))[:30]:
        issues.append({"severity": "error", "message": f"Missing file referenced in {msg}"})
    for msg in sorted(set(wrong_case))[:30]:
        issues.append({"severity": "error", "message": f"Wrong letter case in {msg}; works on Windows, breaks on GitHub Pages."})
    for k, p in rel.items():
        lim = BIG.get(p.suffix.lower())
        size = p.stat().st_size
        if lim and size > lim:
            issues.append({"severity": "warn", "message": f"{k} is {size / 1e6:.1f} MB; compress it for faster loading."})
    for k, data in texts.items():
        if k.endswith(".js"):
            text = data.decode("utf-8", "replace")
            if re.search(r"^\s*debugger\s*;", text, re.M):
                issues.append({"severity": "warn", "message": f"{k}: leftover 'debugger' statement."})
    total = sum(p.stat().st_size for p in files)
    from .studio import verdict
    return {"game_dir": str(d), "files": len(files), "total_mb": round(total / 1e6, 2), "uses_hearth": uses_hearth,
            "issues": issues, "verdict": verdict(issues)}


def package(path: str, version: str = "", name: str = "") -> dict[str, Any]:
    from .studio import safe_name, studio_root
    d = _game_dir(path)
    stem = safe_name(name or d.name) + (f"_{safe_name(version)}" if version else "")
    builds = studio_root() / "builds"
    builds.mkdir(parents=True, exist_ok=True)
    zp = builds / f"{stem}.zip"
    files = sorted(_files(d))
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            if p.name.endswith(".cie.json"):
                continue
            zf.write(p, f"{d.name}/{p.relative_to(d).as_posix()}")
    return {"zip": str(zp), "files": [str(zp)], "file_count": len(files), "zip_mb": round(zp.stat().st_size / 1e6, 3),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "verdict": "pass", "issues": []}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    def wrap(fn: Any, summary: str) -> Any:
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                return {"status": "ok", "summary": f"{summary} Verdict: {data.get('verdict')}.", "data": data}
            except (ValueError, FileNotFoundError, PermissionError) as exc:
                return {"status": "error", "summary": str(exc)}
        return handler

    reg.register(CapabilitySpec(
        id="game.check", area="workflow",
        summary="Check a browser game folder before release: Hearth integration rules, missing or wrong-case asset paths "
                "(GitHub Pages is case-sensitive), oversized files, leftover debugger statements.",
        input_schema={"path": "str"}, permission="read",
        handler=wrap(lambda a: check(a["path"]), "Game checked."), tags=["game", "hearth", "release", "qa"]))
    reg.register(CapabilitySpec(
        id="game.package", area="workflow",
        summary="Zip a browser game folder into CIE's studio builds folder (skips .git, node_modules, build output).",
        input_schema={"path": "str", "version": "optional str", "name": "optional str"}, permission="read",
        handler=wrap(lambda a: package(a["path"], a.get("version", ""), a.get("name", "")), "Game packaged."),
        tags=["game", "release", "zip"]))
