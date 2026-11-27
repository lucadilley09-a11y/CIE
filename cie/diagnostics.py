"""Self-check diagnostics, hardware/resource monitoring, cache inspection, and efficiency metrics."""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .config import get_cie_home, get_vault_root, load_config, project_roots
from .jobs import get_job_manager
from .storage import get_storage


def check_optional_modules() -> dict[str, bool]:
    mods = ["mcp", "yaml", "PIL", "numpy", "scipy", "soundfile", "tree_sitter"]
    out: dict[str, bool] = {}
    for m in mods:
        try:
            importlib.import_module(m)
            out[m] = True
        except Exception:
            out[m] = False
    return out


def _version_key(path: Path) -> tuple:
    import re
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(path.parent.name) + " " + str(path))
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def blender_candidates() -> list[Path]:
    """Usual Windows install places, newest first: the official installer, Steam, the portable zip
    in Downloads/Desktop, and Blender Launcher builds."""
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(env)
        if val:
            roots.append(Path(val))
    home = Path.home()
    found: list[Path] = []
    patterns = [
        ("Blender Foundation", "Blender*/blender.exe"),
        ("Steam/steamapps/common/Blender", "blender.exe"),
        ("Programs/Blender Foundation", "Blender*/blender.exe"),
    ]
    for root in roots:
        for sub, pat in patterns:
            base = root / sub
            if base.is_dir():
                found += [p for p in base.glob(pat) if p.is_file()]
    for extra in (home / "Downloads", home / "Desktop", home / "Blender Launcher"):
        if extra.is_dir():
            found += [p for p in extra.glob("blender-*/blender.exe") if p.is_file()]
            found += [p for p in extra.glob("*/blender-*/blender.exe") if p.is_file()]
    seen, unique = set(), []
    for p in sorted(found, key=_version_key, reverse=True):
        if str(p).lower() not in seen:
            seen.add(str(p).lower())
            unique.append(p)
    return unique


def detect_blender(cfg: dict[str, Any] | None = None) -> str | None:
    cfg = cfg or load_config()
    configured = str(cfg.get("blender_path") or "").strip()
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("blender")
    if found:
        return found
    cands = blender_candidates()
    return str(cands[0]) if cands else None


def hardware_monitor() -> dict[str, Any]:
    home = get_cie_home()
    usage = shutil.disk_usage(home)
    mem_total_mb = None
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            mem_total_mb = round((pages * page_size) / (1024 * 1024), 1)
    except Exception:
        pass
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count() or 1,
        "memory_total_mb": mem_total_mb,
        "disk_free_gb": round(usage.free / (1024**3), 2),
        "disk_total_gb": round(usage.total / (1024**3), 2),
    }


def self_check() -> dict[str, Any]:
    cfg = load_config()
    storage = get_storage()
    fts5_ok = False
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _t USING fts5(a)")
        conn.close()
        fts5_ok = True
    except Exception:
        fts5_ok = False

    vault = get_vault_root()
    vault_writable = os.access(vault, os.W_OK)
    roots = project_roots(cfg)
    root_status = [
        {"id": r["id"], "path": r["path"], "exists": Path(r["path"]).exists()}
        for r in roots
    ]
    mods = check_optional_modules()
    blender = detect_blender(cfg)
    return {
        "healthy": bool(fts5_ok and vault_writable),
        "sqlite_fts5": fts5_ok,
        "vault_path": str(vault),
        "vault_writable": vault_writable,
        "autonomy_level": cfg.get("autonomy_level", "assist"),
        "projects": root_status,
        "optional_modules": mods,
        "blender_detected": blender,
        "index_stats": storage.index_stats(),
        "cache_stats": storage.cache_stats(),
        "jobs_recent": get_job_manager().list_jobs(limit=5),
        "hardware": hardware_monitor(),
    }


def efficiency_metrics() -> dict[str, Any]:
    rows = get_storage().query(
        "SELECT capability_id, elapsed_ms, input_bytes, output_bytes, cache_hit FROM benchmarks ORDER BY id DESC LIMIT 500"
    )
    if not rows:
        return {"total_runs": 0, "cache_hit_ratio": 0.0, "avg_elapsed_ms": 0.0, "p95_elapsed_ms": 0.0}
    elapsed = sorted(float(r["elapsed_ms"]) for r in rows)
    hits = sum(1 for r in rows if r["cache_hit"])
    p95_idx = min(len(elapsed) - 1, int(0.95 * len(elapsed)))
    return {
        "total_runs": len(rows),
        "cache_hit_ratio": round(hits / len(rows), 3),
        "avg_elapsed_ms": round(sum(elapsed) / len(elapsed), 3),
        "p95_elapsed_ms": round(elapsed[p95_idx], 3),
        "total_output_bytes": sum(int(r["output_bytes"]) for r in rows),
    }


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(
        CapabilitySpec(
            id="core.diagnostics",
            area="core",
            summary="Run self-check diagnostics on SQLite FTS5, vault, permissions, modules, and Blender.",
            input_schema={},
            permission="read",
            handler=lambda args: {"status": "ok", "summary": "Diagnostics completed.", "data": self_check()},
            tags=["health", "diagnostics"],
        )
    )
    reg.register(
        CapabilitySpec(
            id="core.hardware",
            area="core",
            summary="Report CPU, RAM, and disk availability.",
            input_schema={},
            permission="read",
            handler=lambda args: {"status": "ok", "summary": "Hardware status collected.", "data": hardware_monitor()},
            tags=["hardware", "resources"],
        )
    )
    reg.register(
        CapabilitySpec(
            id="core.metrics",
            area="core",
            summary="Report measured latency, cache hit ratio, and output sizes.",
            input_schema={},
            permission="read",
            handler=lambda args: {"status": "ok", "summary": "Efficiency metrics collected.", "data": efficiency_metrics()},
            tags=["metrics", "benchmarks"],
        )
    )