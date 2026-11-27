import os
import shutil
import tempfile
import unittest
from pathlib import Path

import dashboard
import mcp_server
from cie import config, jobs, registry, storage


class SecurityAndAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_sec_"))
        os.environ["CIE_HOME"] = str(self.tmp / "cie_home")
        os.environ["CIE_VAULT"] = str(self.tmp / "CIE-Vault")
        storage._DEFAULT_STORAGE = None
        jobs._JOB_MANAGER = None
        registry._REGISTRY = None
        config.save_config({**config.load_config(), "autonomy_level": "autonomous"})

        self.proj = self.tmp / "safe_proj"
        self.proj.mkdir()
        self.target = self.proj / "data.txt"
        self.target.write_text("original content", encoding="utf-8")
        self.outside = self.tmp / "secret.txt"
        self.outside.write_text("top secret", encoding="utf-8")
        config.add_project(str(self.proj), "safe")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blocks_path_traversal_unc_and_symlink_escape(self) -> None:
        with self.assertRaises(PermissionError):
            config.resolve_in_projects(str(self.outside))
        with self.assertRaises(PermissionError):
            config.resolve_in_projects("..\\secret.txt")
        with self.assertRaises(PermissionError):
            config.resolve_in_projects("\\\\server\\share\\file.txt")

        link_path = self.proj / "escape_link.txt"
        try:
            link_path.symlink_to(self.outside)
        except OSError:
            return
        with self.assertRaises(PermissionError):
            config.resolve_in_projects(str(link_path))

    def test_capability_arguments_are_schema_validated(self) -> None:
        reg = registry.get_registry()
        missing = reg.run("audio.compare", {"path_a": "a.wav"})
        self.assertEqual(missing["status"], "error")
        self.assertIn("path_b", missing["summary"])
        wrong_type = reg.run("audio.compare", {"path_a": 42, "path_b": "b.wav"})
        self.assertEqual(wrong_type["status"], "error")
        unknown = reg.run("audio.compare", {"path_a": "a.wav", "path_b": "b.wav", "shell": True})
        self.assertEqual(unknown["status"], "error")
        out_of_range = reg.run("git.log", {"count": 999})
        self.assertEqual(out_of_range["status"], "error")

    def test_default_autonomy_is_assist(self) -> None:
        self.assertEqual(config.default_config()["autonomy_level"], "assist")

    def test_nested_tool_output_secrets_are_redacted(self) -> None:
        from cie.config import redact_value
        redacted = redact_value({"api_key": "plain-secret-value", "nested": ["Bearer abcdefghijklmnop", "sk-1234567890abcdefghij"]})
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertNotIn("abcdefghijklmnop", str(redacted))
        self.assertNotIn("sk-1234567890", str(redacted))

    def test_vault_restore_rejects_path_traversal(self) -> None:
        from cie import vault
        with self.assertRaises(PermissionError):
            vault.restore_note_version("Knowledge/Coding/test.md", "../../../../secret.md")

    def test_old_config_migrates_to_safe_assist_default(self) -> None:
        cfg_path = config.get_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text('{"version":"1.2.0","autonomy_level":"autonomous","projects":[]}', encoding="utf-8")
        migrated = config.load_config()
        self.assertEqual(migrated["autonomy_level"], "assist")
        self.assertEqual(migrated["version"], "2.3.2")

    def test_vault_export_rejects_overlapping_destination(self) -> None:
        from cie import vault
        vault_root = Path(os.environ["CIE_VAULT"]).resolve()
        with self.assertRaises(PermissionError):
            vault.export_vault(str(vault_root / "nested-export"))

    def test_secret_redaction_and_autonomy_gating(self) -> None:
        red = config.redact_secrets("api_key=sk-1234567890abcdefghij")
        self.assertNotIn("sk-1234567890", red)

        # In assist mode, an unapproved write returns approval_required, and MCP callers cannot self-approve.
        config.save_config({**config.load_config(), "autonomy_level": "assist", "approved_tools": []})
        res = mcp_server.cie_run("audio.procedural_sfx", {"output_path": str(self.proj / "x.wav")})
        self.assertEqual(res["status"], "approval_required")
        self.assertTrue(res["approval_required"])
        self.assertFalse(mcp_server.cie_run.__code__.co_varnames.count("approved"))

        # Owner approval (dashboard path) unlocks just that tool.
        config.approve_tool("audio.procedural_sfx")
        res = mcp_server.cie_run("audio.procedural_sfx", {"output_path": str(self.proj / "x.wav")})
        self.assertNotEqual(res["status"], "approval_required")

    def test_observe_mode_remains_read_only_even_with_saved_approval(self) -> None:
        config.save_config({**config.load_config(), "autonomy_level": "observe", "approved_tools": ["audio.procedural_sfx"]})
        result = mcp_server.cie_run("audio.procedural_sfx", {"output_path": str(self.proj / "blocked.wav")})
        self.assertEqual(result["status"], "approval_required")
        self.assertFalse((self.proj / "blocked.wav").exists())

    def test_memory_writes_obey_assist_approval(self) -> None:
        config.save_config({**config.load_config(), "autonomy_level": "assist", "approved_tools": []})
        denied = mcp_server.cie_memory(action="save", title="Should Not Save", body="x")
        self.assertEqual(denied["status"], "approval_required")
        config.approve_tool("memory.save")
        allowed = mcp_server.cie_memory(action="save", title="Approved Note", body="x")
        self.assertEqual(allowed["status"], "ok", msg=str(allowed))

    def test_destructive_memory_actions_require_owner_approval(self) -> None:
        config.save_config({**config.load_config(), "autonomy_level": "autonomous", "approved_tools": []})
        denied = mcp_server.cie_memory(action="export", dest_or_src_dir=str(self.tmp / "out"))
        self.assertEqual(denied["status"], "approval_required")
        config.approve_tool("memory.export")
        allowed = mcp_server.cie_memory(action="export", dest_or_src_dir=str(self.tmp / "out"))
        self.assertEqual(allowed["status"], "ok")

    def test_vault_rejects_sibling_prefix_escape(self) -> None:
        from cie import vault
        vault_dir = Path(os.environ["CIE_VAULT"])
        evil = Path(str(vault_dir) + "-evil")
        with self.assertRaises(PermissionError):
            vault.save_note(title="x", body="y", rel_path=f"../{evil.name}/pwn.md")
        self.assertFalse((evil / "pwn.md").exists())

    def test_corrupt_config_is_backed_up_not_erased(self) -> None:
        cfg_path = config.get_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{not json", encoding="utf-8")
        config.load_config()
        self.assertTrue(cfg_path.with_suffix(".json.corrupt").exists())

    def test_snapshot_and_rollback(self) -> None:
        snap = mcp_server.cie_run("workflow.snapshot_create", {"paths": [str(self.target)], "description": "Before edit"})["data"]
        self.target.write_text("corrupted content", encoding="utf-8")
        # Destructive tools are never approvable over MCP, so restore goes through the registry with explicit approval.
        restored = registry.get_registry().run("workflow.snapshot_restore", {"snapshot_id": snap["snapshot_id"]}, approved=True)
        self.assertEqual(restored["status"], "ok")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "original content")

    def test_dashboard_origin_csrf_policy(self) -> None:
        self.assertTrue(dashboard.is_allowed_origin("http://127.0.0.1:8765", "127.0.0.1:8765"))
        self.assertFalse(dashboard.is_allowed_origin("https://evil.example.com", "127.0.0.1:8765"))


if __name__ == "__main__":
    unittest.main()