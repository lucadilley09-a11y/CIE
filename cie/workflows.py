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
    vault = get_vault_root()
    snap_id = f"snap_{uuid.uuid4().hex[:10]}"
    blob_dir = vault / ".cie" / "snapshots" / snap_id
    blob_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []

    for idx, raw_p in enumerate(paths):
        resolved, root = resolve_in_projects(raw_p)
        if not resolved.exists() or not resolved.is_file():
            continue
        data = resolved.read_bytes()
        h = sha256_bytes(data)
        blob_path = blob_dir / f"{idx}_{h[:12]}.bin"
        blob_path.write_bytes(data)
        manifest.append({
            "original_path": str(resolved),
            "project_id": root["id"],
            "sha256": h,
            "blob_path": str(blob_path),
        })

    get_storage().execute(
        "INSERT INTO snapshots(snapshot_id, project_id, description, manifest_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (snap_id, project_id, description, json.dumps(manifest), utc_now()),
    )
    return {"snapshot_id": snap_id, "files_saved": len(manifest), "description": description}


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    rows = get_storage().query("SELECT manifest_json FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
    if not rows:
        raise FileNotFoundError(f"Snapshot '{snapshot_id}' not found.")
    manifest = json.loads(rows[0]["manifest_json"])
    restored: list[str] = []
    for item in manifest:
        orig, _ = resolve_in_projects(item["original_path"])
        blob = Path(item["blob_path"])
        if blob.exists():
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(blob, orig)
            restored.append(str(orig))
    return {"snapshot_id": snapshot_id, "restored_files": restored, "count": len(restored)}


def list_snapshots(limit: int = 20) -> list[dict[str, Any]]:
    return get_storage().query(
        "SELECT snapshot_id, project_id, description, created_at FROM snapshots ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )


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