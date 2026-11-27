"""Configuration, workspace root safety, autonomy levels, and secret redaction."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUTONOMY_LEVELS = ("observe", "assist", "autonomous")

DEFAULT_EXCLUDES = [
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    "out",
    ".cie",
    ".obsidian",
]
# AI model weights are skipped by file type (DEFAULT_BINARY_EXTS and
# MODEL_WEIGHT_EXTS), not by folder name, so a game's "models/" folder of 3D
# assets is still indexed and snapshotted.
MODEL_WEIGHT_EXTS = {".gguf", ".safetensors", ".pt", ".pth", ".onnx", ".ckpt", ".pb"}

DEFAULT_BINARY_EXTS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".a", ".lib",
    ".gguf", ".safetensors", ".pt", ".pth", ".onnx", ".ckpt", ".pb",
    ".zip", ".tar", ".gz", ".7z", ".rar", ".whl",
    ".pyc", ".pyo", ".class", ".jar",
}

SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_-]{16,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(sk-ant-[A-Za-z0-9_-]{16,})"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"(AKIA[0-9A-Z]{16})"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(ghp_[A-Za-z0-9]{20,})"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{12,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)(password|passwd|secret|api_key|apikey|access_token)\s*[:=]\s*['\"]?([^\s'\"]{6,})['\"]?"), r"\1=[REDACTED]"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def redact_secrets(text: str) -> str:
    if not text:
        return text
    out = text
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_value(value: Any, key: str = "") -> Any:
    """Recursively redact secret-like values in tool results before they reach an LLM client."""
    sensitive_keys = {"password", "passwd", "secret", "client_secret", "secret_key", "api_key", "apikey", "token", "access_token", "refresh_token", "auth_token", "authorization", "private_key"}
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in sensitive_keys:
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def get_cie_home() -> Path:
    """Return the configured CIE data directory, using native OS defaults.

    CIE_HOME always wins. When it is absent, use LocalAppData on Windows,
    Application Support on macOS, and XDG_DATA_HOME on Linux/other systems.
    Preserve an existing legacy ~/.cie directory rather than silently creating
    a second empty data store during an upgrade.
    """
    env_home = os.environ.get("CIE_HOME", "").strip()
    if env_home:
        base = Path(env_home).expanduser()
    elif sys.platform == "win32":
        platform_base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        base = platform_base / "CIE"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "CIE"
    else:
        platform_base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        base = platform_base / "CIE"

    legacy = Path.home() / ".cie"
    if not env_home and not base.exists() and legacy.is_dir():
        base = legacy
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_config_path() -> Path:
    return get_cie_home() / "config.json"


def get_vault_root() -> Path:
    env_vault = os.environ.get("CIE_VAULT", "").strip()
    if env_vault:
        vault = Path(env_vault).expanduser()
    else:
        vault = get_cie_home() / "CIE-Vault"
    vault.mkdir(parents=True, exist_ok=True)
    return vault


def default_config() -> dict[str, Any]:
    return {
        "version": "2.3.2",
        "autonomy_level": "assist",
        "memory_capture_mode": "auto_observed",
        "context_budget_tokens": 6000,
        "context_budget_bytes": 24000,  # legacy compatibility
        "default_detail": "compact",
        "projects": [],
        # Phase 1.3 storage policy (owner decisions 2026-11-27).
        "storage": {
            "auto_snapshot": True,
            "retention_days": 30,
            "limit_gb": 5.0,
        },
        # Phase 1.4: keep the index current while the CIE server runs.
        "knowledge": {
            "background_index": True,
            "interval_sec": 60,
        },
        "allowlisted_commands": {
            "pytest": ["python", "-m", "pytest"],
            "unittest": ["python", "-m", "unittest"],
            "ruff": ["ruff", "check"],
            "mypy": ["mypy"],
        },
        "approved_tools": [],
        "blender_path": "",
        "dashboard": {
            "host": "127.0.0.1",
            "port": 8765,
        },
        "models_dir": str(get_cie_home() / "models"),
    }


def load_config() -> dict[str, Any]:
    cfg_path = get_config_path()
    base = default_config()
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config root must be a JSON object")
            base.update(data)
        except Exception:
            # Keep the unreadable file for recovery instead of silently replacing it
            # (a later save would otherwise erase the user's project list).
            backup = cfg_path.with_suffix(".json.corrupt")
            try:
                cfg_path.replace(backup)
            except OSError:
                pass
    # v1.2.1 safety migration: earlier releases shipped an unsafe autonomous
    # default despite documenting assist as the default. Existing pre-1.2.1
    # configs are moved to assist; owners can opt back in from the dashboard.
    raw_version = str(base.get("version", "0.0.0"))
    version_parts_found = re.findall(r"\d+", raw_version)[:3]
    version_parts = tuple(int(part) for part in version_parts_found)
    version_parts = version_parts + (0,) * (3 - len(version_parts))
    if version_parts < (1, 2, 1):
        base["autonomy_level"] = "assist"
        base["version"] = "1.2.1"
    # Sanitize known fields from legacy or manually edited config files. Unknown
    # keys are preserved for forward compatibility, but malformed known values
    # fall back to safe defaults instead of reaching capability handlers.
    if base.get("autonomy_level") not in AUTONOMY_LEVELS:
        base["autonomy_level"] = "assist"
    if not isinstance(base.get("projects"), list):
        base["projects"] = []
    else:
        base["projects"] = [
            item for item in base["projects"]
            if isinstance(item, str) or (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and bool(item.get("path", "").strip())
            )
        ]
    if not isinstance(base.get("approved_tools"), list) or not all(
        isinstance(item, str) for item in base.get("approved_tools", [])
    ):
        base["approved_tools"] = []
    if not isinstance(base.get("allowlisted_commands"), dict):
        base["allowlisted_commands"] = default_config()["allowlisted_commands"]
    else:
        base["allowlisted_commands"] = {
            str(key): value for key, value in base["allowlisted_commands"].items()
            if isinstance(key, str) and isinstance(value, list)
            and value and all(isinstance(part, str) for part in value)
        }
    for key in ("memory_capture_mode", "default_detail", "blender_path", "models_dir"):
        if not isinstance(base.get(key), str):
            base[key] = default_config()[key]
    for key in ("context_budget_tokens", "context_budget_bytes"):
        value = base.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            base[key] = default_config()[key]
    dashboard = base.get("dashboard")
    if not isinstance(dashboard, dict):
        base["dashboard"] = default_config()["dashboard"]
    else:
        host = dashboard.get("host", "127.0.0.1")
        port = dashboard.get("port", 8765)
        if not isinstance(host, str) or not host.strip():
            host = "127.0.0.1"
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            port = 8765
        base["dashboard"] = {**dashboard, "host": host, "port": port}
    base["version"] = "2.3.2"
    return base


def approve_tool(capability_id: str) -> dict[str, Any]:
    """Owner-only: record approval for a capability in assist mode. Called from the dashboard, never over MCP."""
    cfg = load_config()
    approved = set(cfg.get("approved_tools") or [])
    approved.add(capability_id)
    cfg["approved_tools"] = sorted(approved)
    save_config(cfg)
    return cfg


def revoke_tool(capability_id: str) -> dict[str, Any]:
    cfg = load_config()
    cfg["approved_tools"] = [t for t in (cfg.get("approved_tools") or []) if t != capability_id]
    save_config(cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist configuration without exposing partial JSON."""
    if not isinstance(cfg, dict):
        raise TypeError("Configuration must be a dictionary.")
    normalized = dict(cfg)
    if "version" in normalized and not isinstance(normalized["version"], str):
        raise TypeError("version must be a string.")
    if normalized.get("autonomy_level", "assist") not in AUTONOMY_LEVELS:
        raise ValueError("autonomy_level must be observe, assist, or autonomous.")
    projects = normalized.get("projects", [])
    if not isinstance(projects, list):
        raise TypeError("projects must be a list.")
    for index, project in enumerate(projects):
        if isinstance(project, str):
            if not project.strip():
                raise ValueError(f"projects[{index}] cannot be an empty path.")
            continue
        if not isinstance(project, dict):
            raise TypeError(f"projects[{index}] must be a path string or object.")
        if not isinstance(project.get("path"), str) or not project["path"].strip():
            raise TypeError(f"projects[{index}].path must be a non-empty string.")
        if "id" in project and not isinstance(project["id"], str):
            raise TypeError(f"projects[{index}].id must be a string.")
        if "excludes" in project and (
            not isinstance(project["excludes"], list)
            or not all(isinstance(value, str) for value in project["excludes"])
        ):
            raise TypeError(f"projects[{index}].excludes must be a list of strings.")
    if not isinstance(normalized.get("approved_tools", []), list) or not all(
        isinstance(item, str) for item in normalized.get("approved_tools", [])
    ):
        raise TypeError("approved_tools must be a list of strings.")
    commands = normalized.get("allowlisted_commands", {})
    if not isinstance(commands, dict):
        raise TypeError("allowlisted_commands must be an object.")
    for key, value in commands.items():
        if not isinstance(key, str) or not isinstance(value, list) or not value or not all(
            isinstance(part, str) and part for part in value
        ):
            raise TypeError("allowlisted_commands must map string keys to non-empty string lists.")
    for key in ("memory_capture_mode", "default_detail", "blender_path", "models_dir"):
        if key in normalized and not isinstance(normalized[key], str):
            raise TypeError(f"{key} must be a string.")
    for key in ("context_budget_tokens", "context_budget_bytes"):
        if key in normalized and (
            not isinstance(normalized[key], int)
            or isinstance(normalized[key], bool)
            or normalized[key] <= 0
        ):
            raise ValueError(f"{key} must be a positive integer.")
    if "dashboard" in normalized:
        dashboard = normalized["dashboard"]
        if not isinstance(dashboard, dict):
            raise TypeError("dashboard must be an object.")
        if "host" in dashboard and not isinstance(dashboard["host"], str):
            raise TypeError("dashboard.host must be a string.")
        if "port" in dashboard and (
            not isinstance(dashboard["port"], int)
            or isinstance(dashboard["port"], bool)
            or not 1 <= dashboard["port"] <= 65535
        ):
            raise ValueError("dashboard.port must be an integer between 1 and 65535.")
    cfg_path = get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(cfg_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, cfg_path)
        # Persist the directory entry as well as file contents where supported.
        if os.name != "nt":
            try:
                dir_fd = os.open(cfg_path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # Some filesystems do not support fsync on directory handles.
                pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return normalized


def is_unc_or_network_path(raw_path: str) -> bool:
    s = str(raw_path).strip()
    return s.startswith("\\\\") or s.startswith("//")


def project_roots(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = load_config() if cfg is None else cfg
    projects = cfg.get("projects", [])
    if not isinstance(projects, list):
        return []
    roots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in projects:
        if isinstance(item, str):
            raw_path = item.strip()
            if not raw_path:
                continue
            entry: dict[str, Any] = {"id": Path(raw_path).name, "path": raw_path, "excludes": []}
        elif isinstance(item, dict):
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            entry = dict(item)
            entry.setdefault("id", Path(raw_path).name)
            if not isinstance(entry.get("excludes", []), list) or not all(
                isinstance(value, str) for value in entry.get("excludes", [])
            ):
                continue
        else:
            continue
        if is_unc_or_network_path(raw_path):
            continue
        expanded = Path(raw_path).expanduser()
        if not expanded.is_absolute():
            expanded = get_cie_home() / expanded
        try:
            canonical = str(expanded.resolve())
        except OSError:
            continue
        dedupe_key = os.path.normcase(canonical)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entry["path"] = canonical
        if not isinstance(entry.get("id"), str) or not entry["id"].strip():
            entry["id"] = Path(canonical).name or "project"
        roots.append(entry)
    return roots


def add_project(path_str: str, project_id: str | None = None, excludes: list[str] | None = None) -> dict[str, Any]:
    if is_unc_or_network_path(path_str):
        raise ValueError("UNC/network paths are blocked by security policy.")
    p = Path(path_str).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise ValueError(f"Project path must be an existing directory: {path_str}")
    cfg = load_config()
    pid = (project_id or p.name).strip() or "project"
    roots = project_roots(cfg)
    for r in roots:
        if Path(r["path"]).resolve() == p:
            return r
    entry = {
        "id": pid,
        "path": str(p),
        "excludes": list(excludes or []),
        "added_at": utc_now(),
    }
    roots.append(entry)
    cfg["projects"] = roots
    save_config(cfg)
    return entry


def remove_project(project_id_or_path: str) -> bool:
    cfg = load_config()
    roots = project_roots(cfg)
    kept = [r for r in roots if r.get("id") != project_id_or_path and r.get("path") != project_id_or_path]
    if len(kept) == len(roots):
        return False
    cfg["projects"] = kept
    save_config(cfg)
    return True


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_in_projects(raw_path: str, cfg: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    """Resolve a path strictly inside approved project roots, blocking traversal and symlink escapes."""
    if not isinstance(raw_path, str) or not raw_path.strip() or is_unc_or_network_path(raw_path):
        raise PermissionError("Invalid or network path rejected.")
    if ".." in raw_path.replace("\\", "/").split("/"):
        # Still check canonical resolution, but reject explicit '..' traversal components upfront
        raise PermissionError("Path traversal ('..') is not permitted.")
    cfg = load_config() if cfg is None else cfg
    roots = [entry for entry in project_roots(cfg) if Path(entry["path"]).is_dir()]
    if not roots:
        raise PermissionError("No existing approved project folders configured.")

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        # Try resolving relative to each approved root
        for r in roots:
            root_path = Path(r["path"]).resolve()
            if not root_path.is_dir():
                continue
            joined = root_path / candidate
            resolved = joined.resolve()
            if _is_within(resolved, root_path) and resolved.exists():
                return resolved, r
        # Also allow non-existent target inside first matching root only if explicit parent exists
        for r in roots:
            root_path = Path(r["path"]).resolve()
            if not root_path.is_dir():
                continue
            joined = (root_path / candidate).resolve()
            if _is_within(joined, root_path):
                return joined, r
        raise PermissionError(f"Path '{raw_path}' is outside approved project roots.")

    # Check symlink components along the path before and after resolve
    resolved = candidate.resolve()
    for r in roots:
        root_path = Path(r["path"]).resolve()
        if root_path.is_dir() and _is_within(resolved, root_path):
            return resolved, r
    raise PermissionError(f"Access denied: '{raw_path}' is outside approved project roots.")


def iter_project_files(
    root_entry: dict[str, Any],
    extensions: set[str] | None = None,
    include_binary: bool = False,
) -> list[Path]:
    root_path = Path(root_entry["path"]).resolve()
    if not root_path.is_dir():
        return []
    excludes = set(DEFAULT_EXCLUDES) | set(root_entry.get("excludes") or [])
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in excludes and not Path(dirpath, d).is_symlink()
        )
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if fpath.is_symlink():
                continue
            ext = fpath.suffix.lower()
            if not include_binary and ext in DEFAULT_BINARY_EXTS:
                continue
            if extensions and ext not in extensions:
                continue
            try:
                resolved = fpath.resolve()
            except OSError:
                continue
            if _is_within(resolved, root_path):
                results.append(resolved)
    return results