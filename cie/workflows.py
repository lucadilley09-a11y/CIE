"""Content-addressed snapshot/rollback system and multi-step workflow runner."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import get_vault_root, resolve_in_projects, utc_now
from .storage import get_storage, sha256_bytes


def create_snapshot(paths: list[str], description: str = "Pre-change snapshot", project_id: str = "default") -> dict[str, Any]:
    """Backward-compatible wrapper over the Phase 1.3 deduplicated store."""
    from .snapstore import create_snapshot as _create

    return _create(paths=paths, description=description, kind="manual")


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    """Backward-compatible wrapper: preview-checked restore with a safety snapshot."""
    from .snapstore import restore_snapshot as _restore

    result = _restore(snapshot_id)
    if "restored_files" not in result:
        result["count"] = result.get("restored", 0)
    return result


def list_snapshots(limit: int = 20) -> list[dict[str, Any]]:
    from .snapstore import list_snapshots as _list

    return _list(limit=limit)["snapshots"]


def run_workflow(steps: list[dict[str, Any]], approved: bool = False) -> dict[str, Any]:
    from .registry import get_registry

    reg = get_registry()
    results = []
    for idx, step in enumerate(steps):
        cid = str(step.get("capability_id", ""))
        args = step.get("args") or {}
        res = reg.run(cid, args, approved=approved)
        results.append({"step": idx + 1, "capability_id": cid, "status": res["status"], "summary": res["summary"], "data": res["data"]})
        if res["status"] in {"error", "approval_required"}:
            return {"completed_steps": idx, "total_steps": len(steps), "halted_at": idx + 1, "results": results}
    return {"completed_steps": len(steps), "total_steps": len(steps), "results": results}


def register_capabilities(reg: Any) -> None:
    from .registry import CapabilitySpec

    reg.register(CapabilitySpec(
        id="workflow.snapshot_create",
        area="workflow",
        summary="Create a content-addressed backup snapshot of approved files before edits.",
        input_schema={"paths": "list[str]", "description": "optional str"},
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Snapshot created.", "data": create_snapshot(args.get("paths") or [], args.get("description", "Pre-change snapshot"))},
        tags=["snapshot", "backup"],
    ))
    reg.register(CapabilitySpec(
        id="workflow.snapshot_restore",
        area="workflow",
        summary="Restore files byte-for-byte from a previously created snapshot.",
        input_schema={"snapshot_id": "str"},
        permission="write",
        destructive=True,
        handler=lambda args: {"status": "ok", "summary": "Snapshot restored.", "data": restore_snapshot(args["snapshot_id"])},
        tags=["snapshot", "rollback"],
    ))
    reg.register(CapabilitySpec(
        id="workflow.run",
        area="workflow",
        summary="Execute a multi-step declared capability workflow.",
        input_schema={"steps": "list[{capability_id, args}]"},
        # Steps can write or execute, so the runner itself must be gated like a write.
        permission="write",
        handler=lambda args: {"status": "ok", "summary": "Workflow executed.", "data": run_workflow(args.get("steps") or [])},
        tags=["workflow", "automation"],
    ))