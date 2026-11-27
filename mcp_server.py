"""CIE MCP Server exposing the 4-primitive contract: cie_discover, cie_run, cie_memory, cie_status."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from cie.config import load_config, project_roots
from cie.diagnostics import self_check
from cie.jobs import get_job_manager
from cie.registry import get_registry


def cie_discover(area: str = "", keyword: str = "", project: str = "") -> dict[str, Any]:
    """Discover CIE capabilities by area, keyword, or project.

    Shortcuts: for a multi-step task, call cie_run("orchestrate.plan", {"task": ..., "project_id": ...})
    to get a ready-made step list, then cie_run("orchestrate.run", {"steps": [...]}).
    For project context, start with cie_run("knowledge.summary", {"project_id": ...}).
    Before editing project files directly, call cie_run("storage.snapshot", {"paths": [...]}) so the
    change can be undone with storage.restore_preview and storage.restore.
    """
    caps = get_registry().discover(area=area, keyword=keyword, project=project)
    return {
        "status": "ok",
        "count": len(caps),
        "capabilities": caps,
    }


def cie_run(
    capability_id: str,
    args: dict[str, Any] | None = None,
    detail: str = "compact",
) -> dict[str, Any]:
    """Execute a registered CIE capability by ID with structured output and permission gating.

    Approval is never accepted from the caller: the MCP client cannot approve its own actions.
    Approvals come from the owner via the dashboard (assist mode) or autonomy settings.
    """
    return get_registry().run(
        capability_id=capability_id,
        args=args or {},
        detail=detail,
        permission_mode="auto",
        approved=False,
    )


def cie_memory(
    action: str,
    title: str = "",
    body: str = "",
    query: str = "",
    folder: str = "Knowledge/Coding",
    project: str = "",
    tags: list[str] | None = None,
    rel_path: str = "",
    version_file: str = "",
    dest_or_src_dir: str = "",
    confidence: str = "observed",
    limit: int = 10,
) -> dict[str, Any]:
    """Access the Obsidian-compatible memory vault with normal CIE permission checks."""
    act = action.strip().lower()
    aliases = {"update": "save"}
    act = aliases.get(act, act)
    all_args = {
        "title": title, "body": body, "query": query, "folder": folder,
        "project": project, "tags": tags or [], "rel_path": rel_path,
        "version_file": version_file, "dest_or_src_dir": dest_or_src_dir,
        "confidence": confidence, "limit": limit,
    }
    action_fields = {
        "search": ("query", "project", "limit"),
        "save": ("title", "body", "folder", "project", "tags", "confidence", "rel_path"),
        "daily": ("body", "title"),
        "backlinks": ("rel_path", "title", "query"),
        "review": (),
        "versions": ("rel_path",),
        "restore": ("rel_path", "version_file"),
        "export": ("dest_or_src_dir",),
        "import": ("dest_or_src_dir",),
    }
    action_args = {key: all_args[key] for key in action_fields.get(act, ()) if all_args[key] not in ("", None)}
    if act == "save":
        action_args["tags"] = tags or []
    if act == "save" and not title:
        return {"status": "error", "summary": "Title is required to save a note."}
    if act == "restore" and (not rel_path or not version_file):
        return {"status": "error", "summary": "Both rel_path and version_file are required."}
    if act in {"import", "export"} and not dest_or_src_dir:
        return {"status": "error", "summary": "dest_or_src_dir is required."}
    if act not in {"search", "save", "daily", "backlinks", "review", "versions", "restore", "export", "import"}:
        return {"status": "error", "summary": f"Unknown memory action '{action}'."}
    result = get_registry().run(f"memory.{act}", action_args, permission_mode="auto", approved=False)
    if result.get("status") != "ok":
        return result
    payload = result.get("data") or {}
    return {"status": "ok", **payload}


def cie_status(job_id: str = "") -> dict[str, Any]:
    """Inspect CIE health, jobs, index status, enabled modules, and local resources."""
    if job_id:
        job = get_job_manager().status(job_id)
        return {"status": "ok" if job else "error", "job": job}
    return {"status": "ok", "health": self_check()}


def get_resource(uri: str) -> str:
    if uri == "cie://catalog":
        return json.dumps(cie_discover(), indent=2)
    if uri == "cie://projects":
        return json.dumps({"projects": project_roots(load_config())}, indent=2)
    return json.dumps({"error": f"Unknown resource {uri}"})


PROMPT_TEMPLATES = {
    "inspect_repository": "Call cie_discover(area='efficiency') and cie_run('eff.file_map') then cie_run('coding.brain') to map the repository before reading individual files.",
    "diagnose_build_failure": "Call cie_run('eff.errors_only') or cie_run('coding.stack_trace') on the failure log, then search cie_memory(action='search') for prior fixes.",
    "audit_audio_asset": "Call cie_run('audio.inspector') and cie_run('audio.silence_clipping') on the target WAV file.",
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cie-mcp", description="CIE MCP server")
    parser.add_argument("--stdio", action="store_true", help="Run the MCP stdio transport")
    parser.add_argument("--version", action="store_true", help="Print CIE version and exit")
    parser.add_argument("--doctor", action="store_true", help="Run health checks and exit")
    args = parser.parse_args(argv)
    if args.version:
        from cie import __version__
        print(__version__)
        return 0
    if args.doctor:
        result = self_check()
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("healthy") else 1
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        sys.stderr.write("MCP SDK ('mcp') is not installed. Install the CIE runtime dependencies first.\n")
        return 1

    mcp = FastMCP("creative-intelligence-engine")
    mcp.tool()(cie_discover)
    mcp.tool()(cie_run)
    mcp.tool()(cie_memory)
    mcp.tool()(cie_status)

    @mcp.resource("cie://catalog")
    def _res_catalog() -> str:
        return get_resource("cie://catalog")

    @mcp.resource("cie://projects")
    def _res_projects() -> str:
        return get_resource("cie://projects")

    @mcp.prompt()
    def inspect_repository() -> str:
        return PROMPT_TEMPLATES["inspect_repository"]

    @mcp.prompt()
    def diagnose_build_failure() -> str:
        return PROMPT_TEMPLATES["diagnose_build_failure"]

    @mcp.prompt()
    def audit_audio_asset() -> str:
        return PROMPT_TEMPLATES["audit_audio_asset"]

    try:
        from cie.knowledge import start_background_indexer

        start_background_indexer()
    except Exception as exc:  # noqa: BLE001 - the server must start even if indexing cannot
        sys.stderr.write(f"CIE background indexer did not start: {exc}\n")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()