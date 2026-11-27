"""Capability registry, structured response envelope, permission gating, and batch execution."""

from __future__ import annotations

import importlib.util
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import load_config, redact_secrets, redact_value
from .storage import get_storage


@dataclass
class CapabilitySpec:
    id: str
    area: str
    summary: str
    input_schema: dict[str, Any]
    permission: str  # "read" | "write" | "exec"
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    version: str = "1.0.0"
    destructive: bool = False
    tags: list[str] = field(default_factory=list)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, CapabilitySpec] = {}

    def register(self, spec: CapabilitySpec) -> None:
        self._caps[spec.id] = spec

    def get(self, capability_id: str) -> CapabilitySpec | None:
        return self._caps.get(capability_id)

    def discover(self, area: str = "", keyword: str = "", project: str = "") -> list[dict[str, Any]]:
        area_l = area.strip().lower()
        kw_l = keyword.strip().lower()
        out: list[dict[str, Any]] = []
        for cap in sorted(self._caps.values(), key=lambda c: c.id):
            if area_l and cap.area.lower() != area_l:
                continue
            if kw_l:
                hay = f"{cap.id} {cap.area} {cap.summary} {' '.join(cap.tags)}".lower()
                if kw_l not in hay:
                    continue
            out.append({
                "id": cap.id,
                "area": cap.area,
                "summary": cap.summary,
                "permission": cap.permission,
                "destructive": cap.destructive,
                "input_schema": cap.input_schema,
            })
        return out

    def check_permission(self, spec: CapabilitySpec, approved: bool = False) -> tuple[bool, str]:
        cfg = load_config()
        level = cfg.get("autonomy_level", "assist")
        approved_tools = set(cfg.get("approved_tools") or [])
        owner_approved = spec.id in approved_tools
        if spec.permission == "read":
            return True, ""
        # Observe is strictly read-only; a saved allowlist entry cannot override it.
        if level == "observe":
            return False, f"Autonomy level is 'observe'; write/exec capability '{spec.id}' is blocked."
        # Destructive operations always need an explicit owner-side approval.
        if spec.destructive and not (approved or owner_approved):
            return False, f"Capability '{spec.id}' is destructive and requires owner approval in the dashboard."
        if level == "assist" and not (approved or owner_approved):
            return False, f"Autonomy level is 'assist'; '{spec.id}' requires owner approval in the dashboard."
        return True, ""

    @staticmethod
    def _validate_args(spec: CapabilitySpec, args: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        schema = spec.input_schema or {}
        unknown = sorted(set(args) - set(schema))
        if unknown:
            errors.append(f"Unknown argument(s): {', '.join(unknown)}")
        for name, raw_type in schema.items():
            type_spec = str(raw_type).strip()
            optional = type_spec.startswith("optional ") or type_spec.startswith("legacy optional ")
            type_spec = type_spec.replace("legacy optional ", "", 1) if type_spec.startswith("legacy optional ") else type_spec
            if type_spec.startswith("optional "):
                type_spec = type_spec[len("optional "):]
            if name not in args or args[name] is None:
                if not optional and name not in args:
                    errors.append(f"Missing required argument: {name}")
                elif not optional and args.get(name) is None:
                    errors.append(f"Argument '{name}' cannot be null")
                continue
            value = args[name]
            base = type_spec.split(" ", 1)[0]
            valid = True
            if base == "str" or base == "owner/name" or base == "pr|issue":
                valid = isinstance(value, str)
                if valid and base == "pr|issue":
                    valid = value in {"pr", "issue"}
            elif base == "int":
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif base == "float":
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            elif base == "bool":
                valid = isinstance(value, bool)
            elif base == "list[str]":
                valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
            elif base == "list[dict]" or base.startswith("list[{"):
                valid = isinstance(value, list) and all(isinstance(item, dict) for item in value)
            elif base == "list":
                valid = isinstance(value, list)
            elif base == "object":
                valid = isinstance(value, dict)
            if valid and base in {"int", "float"}:
                bounds = re.search(r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)", type_spec)
                if bounds:
                    low, high = float(bounds.group(1)), float(bounds.group(2))
                    valid = low <= float(value) <= high
            if not valid:
                errors.append(f"Argument '{name}' must be {type_spec}")
        try:
            if len(json.dumps(args, ensure_ascii=False, default=str).encode("utf-8")) > 2_000_000:
                errors.append("Arguments exceed the 2 MB safety limit")
        except Exception:
            errors.append("Arguments must be JSON-serializable")
        return errors

    def run(
        self,
        capability_id: str,
        args: dict[str, Any] | None = None,
        detail: str = "compact",
        permission_mode: str = "auto",
        approved: bool = False,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return make_envelope(status="error", capability=capability_id, summary="Arguments must be a JSON object.", elapsed_ms=(time.perf_counter() - t0) * 1000.0)
        args = dict(args)
        spec = self.get(capability_id)
        if spec is None:
            return make_envelope(
                status="error",
                capability=capability_id,
                summary=f"Unknown capability '{capability_id}'. Use cie_discover first.",
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        validation_errors = self._validate_args(spec, args)
        if validation_errors:
            return make_envelope(
                status="error", capability=capability_id,
                summary="Invalid arguments: " + "; ".join(validation_errors),
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        is_approved = approved or (permission_mode == "approved")
        allowed, reason = self.check_permission(spec, approved=is_approved)
        if not allowed:
            return make_envelope(
                status="approval_required",
                capability=capability_id,
                summary=reason,
                approval_required=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        auto_snap: dict[str, Any] | None = None
        try:
            from .snapstore import auto_snapshot_for

            auto_snap = auto_snapshot_for(capability_id, spec.permission, args)
        except Exception as exc:  # noqa: BLE001 - never write without the safety net
            return make_envelope(
                status="error",
                capability=capability_id,
                summary=redact_secrets(
                    f"Automatic snapshot failed, so the change was not made: {exc}. "
                    "Fix the cause, or set storage.auto_snapshot to false in CIE's config."
                ),
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

        try:
            res = spec.handler(args)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            status = res.get("status", "ok")
            summary = redact_secrets(str(res.get("summary", f"Completed {capability_id}")))
            data = redact_value(res.get("data", {}))
            evidence = redact_value(res.get("evidence", []))
            if auto_snap:
                evidence = list(evidence) + [{
                    "auto_snapshot": auto_snap["snapshot_id"],
                    "files": auto_snap["files_saved"] + auto_snap["files_absent"],
                    "undo": f"storage.restore_preview then storage.restore with snapshot_id {auto_snap['snapshot_id']}",
                }]
            artifacts = redact_value(res.get("artifacts", []))
            warnings = redact_value(res.get("warnings", []))
            cache_meta = res.get("cache", {"hit": False})
            budget_meta = res.get("budget", {"truncated": False, "omitted_items": 0})
            if detail == "summary" and isinstance(data, dict) and len(str(data)) > 2000:
                budget_meta = {"truncated": True, "omitted_items": len(data)}
                data = {"keys": list(data.keys())}

            env = make_envelope(
                status=status,
                capability=capability_id,
                summary=summary,
                data=data,
                evidence=evidence,
                artifacts=artifacts,
                warnings=warnings,
                cache=cache_meta,
                budget=budget_meta,
                elapsed_ms=elapsed_ms,
            )
            try:
                get_storage().record_benchmark(
                    capability_id=capability_id,
                    elapsed_ms=elapsed_ms,
                    input_bytes=len(str(args)),
                    output_bytes=len(str(env)),
                    cache_hit=bool(cache_meta.get("hit")),
                )
            except Exception:
                pass
            return env
        except PermissionError as exc:
            return make_envelope(
                status="error",
                capability=capability_id,
                summary=f"Permission denied: {exc}",
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )
        except Exception as exc:
            return make_envelope(
                status="error",
                capability=capability_id,
                summary=redact_secrets(f"Execution failed: {exc}"),
                elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            )

    def load_plugin(self, plugin_path: str) -> list[str]:
        p = Path(plugin_path).resolve()
        if not p.exists() or p.suffix != ".py":
            raise ValueError("Plugin must be an existing .py file.")
        spec = importlib.util.spec_from_file_location(f"cie_plugin_{p.stem}", str(p))
        if spec is None or spec.loader is None:
            raise ValueError("Unable to load plugin spec.")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "register_capabilities"):
            raise ValueError("Plugin must define register_capabilities(registry).")
        before = set(self._caps.keys())
        mod.register_capabilities(self)
        return sorted(set(self._caps.keys()) - before)


def make_envelope(
    status: str,
    capability: str,
    summary: str,
    data: Any = None,
    evidence: list[Any] | None = None,
    artifacts: list[str] | None = None,
    warnings: list[str] | None = None,
    approval_required: bool = False,
    elapsed_ms: float = 0.0,
    cache: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "capability": capability,
        "summary": summary,
        "data": data if data is not None else {},
        "evidence": evidence or [],
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "approval_required": approval_required,
        "elapsed_ms": round(elapsed_ms, 3),
        "cache": cache or {"hit": False},
        "budget": budget or {"truncated": False, "omitted_items": 0},
    }


_REGISTRY: CapabilityRegistry | None = None


def get_registry() -> CapabilityRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CapabilityRegistry()
        register_all_capabilities(_REGISTRY)
    return _REGISTRY


def register_all_capabilities(reg: CapabilityRegistry) -> None:
    """Register all CIE capabilities across core, efficiency, coding, art, audio, models, workflows."""
    from . import art, audio, coding, creative, diagnostics, efficiency, external, models, workflows

    diagnostics.register_capabilities(reg)
    efficiency.register_capabilities(reg)
    coding.register_capabilities(reg)
    art.register_capabilities(reg)
    audio.register_capabilities(reg)
    models.register_capabilities(reg)
    workflows.register_capabilities(reg)
    from . import vault
    vault.register_capabilities(reg)
    external.register_capabilities(reg)
    creative.register_capabilities(reg)
    from . import projects
    projects.register_capabilities(reg)
    from . import snapstore
    snapstore.register_capabilities(reg)
    from . import knowledge
    knowledge.register_capabilities(reg)
    from . import orchestrate
    orchestrate.register_capabilities(reg)
    from . import routing
    routing.register_capabilities(reg)
    from . import templates
    templates.register_capabilities(reg)

    def _batch_handler(args: dict[str, Any]) -> dict[str, Any]:
        ops = args.get("operations") or []
        if not isinstance(ops, list) or not ops:
            return {"status": "error", "summary": "Provide a non-empty 'operations' list."}
        results = []
        for item in ops[:25]:
            cid = str(item.get("capability_id", ""))
            sub_args = item.get("args") or {}
            spec = reg.get(cid)
            if spec is None or spec.permission != "read":
                results.append({
                    "capability_id": cid,
                    "status": "error",
                    "summary": "Batch runner only allows registered read-only capabilities.",
                })
                continue
            results.append(reg.run(cid, sub_args, detail=str(args.get("detail", "compact"))))
        return {
            "status": "ok",
            "summary": f"Executed {len(results)} batched read-only operations.",
            "data": {"results": results},
        }

    reg.register(
        CapabilitySpec(
            id="core.batch",
            area="core",
            summary="Run multiple safe read-only operations in one MCP call.",
            input_schema={"operations": "list[{capability_id, args}]", "detail": "optional str"},
            permission="read",
            handler=_batch_handler,
            tags=["batch", "efficiency"],
        )
    )