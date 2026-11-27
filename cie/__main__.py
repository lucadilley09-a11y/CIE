"""Command-line interface for CIE."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__
from .config import get_cie_home, get_vault_root, load_config, project_roots
from .diagnostics import self_check


def _doctor() -> int:
    print(f"CIE {__version__}")
    print(f"Home:  {get_cie_home()}")
    print(f"Vault: {get_vault_root()}")
    try:
        result = self_check()
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Doctor failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("healthy") else 1


def _config() -> int:
    cfg = load_config()
    print(json.dumps(cfg, indent=2, default=str))
    print(f"\n{len(project_roots(cfg))} project(s) configured.")
    return 0


def _project(args: argparse.Namespace) -> int:
    from . import projects

    try:
        if args.action == "add":
            result = projects.register_project(args.path, args.id or "", args.exclude or [])
        elif args.action == "list":
            result = projects.list_projects()
        elif args.action == "detect":
            result = projects.detect_project(args.path)
        elif args.action == "remove":
            result = projects.remove_project(args.id)
        else:
            return 2
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


def _approve(run_id: str, yes: bool) -> int:
    """Owner approval for a workflow run: shows every change it can make, then asks."""
    from . import flows

    try:
        run = flows.load_run(run_id)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Workflow '{run['name']}' (run {run_id}), created {run.get('created')}")
    print(f"Inputs: {json.dumps(run['inputs'], default=str)[:600]}")
    print(f"It reads/makes {run['plan']['reads']} things in CIE's studio and can change your files with these steps:")
    for c in run["plan"]["changes"]:
        print(f"  - {c['step']}: {c['capability']} ({c['permission']}) {json.dumps(c['args'], default=str)[:300]}")
    print("Every change is snapshotted first; if the run fails it is rolled back.")
    if run["status"] != "awaiting_approval":
        print(f"This run is '{run['status']}', so there is nothing to approve.")
        return 1
    if not yes:
        answer = input("Approve this run? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not approved.")
            return 1
    flows.approve(run_id)
    print("Approved. Tell Claude to continue (it calls flow.resume).")
    return 0


def _runs() -> int:
    from . import flows

    for r in flows.history(30):
        print(f"{r['run_id']}  {r['status']:<18} {r['name']:<18} changes={r['changes']}  {r.get('created') or ''}")
    return 0


def _serve(transport: str) -> int:
    if transport != "stdio":
        print("Only stdio transport is supported by this build.", file=sys.stderr)
        return 2
    try:
        from mcp_server import main as mcp_main
    except ImportError as exc:
        print(f"MCP server dependencies are unavailable: {exc}", file=sys.stderr)
        return 2
    return mcp_main(["--stdio"])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cie", description="Creative Intelligence Engine")
    parser.add_argument("--version", action="version", version=f"cie {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check CIE health and local capabilities")
    sub.add_parser("config", help="Print current configuration")
    proj = sub.add_parser("project", help="Register, list, detect, or remove projects")
    psub = proj.add_subparsers(dest="action", required=True)
    padd = psub.add_parser("add", help="Register a project folder")
    padd.add_argument("path")
    padd.add_argument("--id", default="")
    padd.add_argument("--exclude", action="append", help="Folder name to skip (repeatable)")
    psub.add_parser("list", help="List registered projects")
    pdet = psub.add_parser("detect", help="Detect a folder's project type")
    pdet.add_argument("path")
    prem = psub.add_parser("remove", help="Unregister a project (files are not touched)")
    prem.add_argument("id")
    appr = sub.add_parser("approve", help="Approve a workflow run that will change files (shows the plan first)")
    appr.add_argument("run_id")
    appr.add_argument("--yes", action="store_true", help="Skip the y/N question")
    sub.add_parser("runs", help="List recent workflow runs")
    serve = sub.add_parser("serve", help="Run the MCP server over stdio")
    serve.add_argument("--transport", choices=("stdio",), default="stdio")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor()
    if args.command == "config":
        return _config()
    if args.command == "project":
        return _project(args)
    if args.command == "approve":
        return _approve(args.run_id, args.yes)
    if args.command == "runs":
        return _runs()
    if args.command == "serve":
        return _serve(args.transport)
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
