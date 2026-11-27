"""Loopback-only CIE owner dashboard with per-session token and strict Origin/CSRF checks."""

from __future__ import annotations

import html
import json
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from cie.config import add_project, approve_tool, load_config, project_roots, remove_project, revoke_tool, save_config
from cie.diagnostics import self_check
from cie.models import list_models
from cie.registry import get_registry
from cie.storage import get_storage
from cie.vault import review_vault, search_notes

SESSION_TOKEN = secrets.token_urlsafe(24)


def is_allowed_origin(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.hostname in {"127.0.0.1", "localhost"} and (not host or parsed.netloc == host)


def _approval_button(cap_id: str, is_approved: bool, token: str) -> str:
    action = "revoke" if is_approved else "approve"
    label = "Revoke" if is_approved else "Approve"
    return (
        f"<form method='post' action='/api/approve?token={html.escape(token)}' style='margin:0'>"
        f"<input type='hidden' name='capability' value='{html.escape(cap_id)}'>"
        f"<button name='action' value='{action}'>{label}</button></form>"
    )


def render_dashboard_html(token: str) -> str:
    health = self_check()
    cfg = load_config()
    projects = project_roots(cfg)
    notes = search_notes("", limit=20)
    models = list_models()["models"]
    caps = get_registry().discover()
    cache = get_storage().cache_stats()["summary"]

    proj_rows = "".join(
        f"<tr><td>{html.escape(p['id'])}</td><td><code>{html.escape(p['path'])}</code></td></tr>"
        for p in projects
    ) or "<tr><td colspan='2'>No approved projects yet.</td></tr>"

    note_rows = "".join(
        f"<tr><td>{html.escape(n['title'])}</td><td>{html.escape(n.get('project',''))}</td><td>{html.escape(n.get('confidence',''))}</td><td><code>{html.escape(n['rel_path'])}</code></td></tr>"
        for n in notes
    ) or "<tr><td colspan='4'>Vault is empty.</td></tr>"

    approved = set(cfg.get("approved_tools") or [])
    approval_rows = "".join(
        f"<tr><td><code>{html.escape(c['id'])}</code></td><td>{html.escape(c['permission'])}</td>"
        f"<td>{'Approved' if c['id'] in approved else 'Not approved'}</td>"
        f"<td>{'' if c['destructive'] or c['permission'] == 'read' else _approval_button(c['id'], c['id'] in approved, token)}</td></tr>"
        for c in caps if c["permission"] != "read" or c["destructive"]
    ) or "<tr><td colspan='4'>No write or exec tools.</td></tr>"

    model_rows = "".join(
        f"<tr><td>{html.escape(m['name'])}</td><td>{html.escape(m['license'])}</td><td>{m['size_mb']} MB</td><td>{'Installed' if m['installed'] else 'Not installed (manual)'}</td></tr>"
        for m in models
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CIE Owner Dashboard</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 24px; background: #0f172a; color: #e2e8f0; }}
  h1, h2 {{ color: #38bdf8; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 14px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; background: #1e293b; }}
  th, td {{ border: 1px solid #334155; padding: 8px 10px; text-align: left; font-size: 14px; }}
  th {{ background: #0f172a; color: #94a3b8; }}
  input, select, button {{ padding: 8px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #f8fafc; }}
  button {{ background: #0284c7; border: none; cursor: pointer; font-weight: 600; }}
</style>
</head>
<body>
<h1>Creative Intelligence Engine — Owner Dashboard</h1>
<div class="grid">
  <div class="card"><strong>Health</strong><br>{'Healthy' if health['healthy'] else 'Attention needed'} (FTS5: {health['sqlite_fts5']})</div>
  <div class="card"><strong>Autonomy</strong><br>{html.escape(cfg.get('autonomy_level', 'assist'))}</div>
  <div class="card"><strong>Capabilities</strong><br>{len(caps)} registered</div>
  <div class="card"><strong>Cache</strong><br>{cache['entries']} entries / {cache['total_hits']} hits</div>
</div>

<h2>Approved Projects</h2>
<form method="post" action="/api/projects?token={html.escape(token)}">
  <input type="text" name="path" placeholder="C:\\path\\to\\project" size="45" required>
  <input type="text" name="project_id" placeholder="project-id (optional)" size="20">
  <button type="submit">Add Project</button>
</form>
<br>
<table><thead><tr><th>ID</th><th>Path</th></tr></thead><tbody>{proj_rows}</tbody></table>

<h2>Autonomy Mode</h2>
<form method="post" action="/api/autonomy?token={html.escape(token)}">
  <select name="level">
    <option value="observe" {'selected' if cfg.get('autonomy_level')=='observe' else ''}>Observe (read-only)</option>
    <option value="assist" {'selected' if cfg.get('autonomy_level')=='assist' else ''}>Assist (recommended)</option>
    <option value="autonomous" {'selected' if cfg.get('autonomy_level')=='autonomous' else ''}>Autonomous (opt-in)</option>
  </select>
  <button type="submit">Save Mode</button>
</form>

<h2>Approved Tools (assist mode)</h2>
<p>Write and exec tools need owner approval in assist mode. Destructive tools require explicit owner pre-approval even in autonomous mode; approve only tools you understand.</p>
<table><thead><tr><th>Capability</th><th>Permission</th><th>Status</th><th></th></tr></thead><tbody>{approval_rows}</tbody></table>

<h2>Obsidian Memory Vault (Recent)</h2>
<table><thead><tr><th>Title</th><th>Project</th><th>Confidence</th><th>Path</th></tr></thead><tbody>{note_rows}</tbody></table>

<h2>Optional Local Models (Never Auto-Downloaded)</h2>
<table><thead><tr><th>Model</th><th>License</th><th>Size</th><th>Status</th></tr></thead><tbody>{model_rows}</tbody></table>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self, parsed_qs: dict[str, list[str]]) -> bool:
        hdr = self.headers.get("X-CIE-Token", "")
        qtok = (parsed_qs.get("token") or [""])[0]
        return secrets.compare_digest(hdr or qtok, SESSION_TOKEN)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if not self._authorized(qs):
            self._send(403, json.dumps({"error": "Invalid or missing session token."}), "application/json")
            return
        if parsed.path == "/api/status":
            self._send(200, json.dumps(self_check(), default=str), "application/json")
            return
        if parsed.path == "/api/vault/review":
            self._send(200, json.dumps(review_vault(), default=str), "application/json")
            return
        self._send(200, render_dashboard_html(SESSION_TOKEN))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if not is_allowed_origin(self.headers.get("Origin"), self.headers.get("Host")):
            self._send(403, json.dumps({"error": "Origin rejected by CSRF policy."}), "application/json")
            return
        if not self._authorized(qs):
            self._send(403, json.dumps({"error": "Invalid or missing session token."}), "application/json")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 65536)).decode("utf-8", errors="replace")
        form = parse_qs(raw)
        if parsed.path == "/api/projects":
            path_val = (form.get("path") or [""])[0]
            pid_val = (form.get("project_id") or [""])[0] or None
            try:
                add_project(path_val, pid_val)
                self.send_response(303)
                self.send_header("Location", f"/?token={SESSION_TOKEN}")
                self.end_headers()
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}), "application/json")
            return
        if parsed.path == "/api/approve":
            cap_id = (form.get("capability") or [""])[0]
            action = (form.get("action") or [""])[0]
            spec = get_registry().get(cap_id)
            if spec is None or spec.permission == "read":
                self._send(400, json.dumps({"error": "Only registered write/exec tools can be approved."}), "application/json")
                return
            if action == "approve":
                approve_tool(cap_id)
            elif action == "revoke":
                revoke_tool(cap_id)
            else:
                self._send(400, json.dumps({"error": "action must be approve or revoke"}), "application/json")
                return
            self.send_response(303)
            self.send_header("Location", f"/?token={SESSION_TOKEN}")
            self.end_headers()
            return
        if parsed.path == "/api/autonomy":
            lvl = (form.get("level") or ["assist"])[0]
            if lvl in {"observe", "assist", "autonomous"}:
                cfg = load_config()
                cfg["autonomy_level"] = lvl
                save_config(cfg)
            self.send_response(303)
            self.send_header("Location", f"/?token={SESSION_TOKEN}")
            self.end_headers()
            return
        self._send(404, json.dumps({"error": "Not found"}), "application/json")

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    cfg = load_config()
    port = int(cfg.get("dashboard", {}).get("port", 8765))
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"CIE Dashboard listening at http://127.0.0.1:{port}/?token={SESSION_TOKEN}")
    server.serve_forever()


if __name__ == "__main__":
    main()