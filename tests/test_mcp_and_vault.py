import os
import shutil
import tempfile
import unittest
from pathlib import Path

import mcp_server
from cie import config, jobs, registry, storage, vault


class McpAndVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_vault_"))
        os.environ["CIE_HOME"] = str(self.tmp / "cie_home")
        os.environ["CIE_VAULT"] = str(self.tmp / "CIE-Vault")
        storage._DEFAULT_STORAGE = None
        jobs._JOB_MANAGER = None
        registry._REGISTRY = None
        config.save_config({**config.load_config(), "autonomy_level": "autonomous"})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mcp_discover_and_status(self) -> None:
        disc = mcp_server.cie_discover(area="coding")
        self.assertEqual(disc["status"], "ok")
        self.assertGreater(disc["count"], 5)
        st = mcp_server.cie_status()
        self.assertTrue(st["health"]["healthy"])

    def test_vault_wikilinks_backlinks_versions_and_export(self) -> None:
        mcp_server.cie_memory(action="save", title="Audio Pipeline", body="Uses [[WAV Inspector]] for QA.", folder="Knowledge/Audio", tags=["audio"])
        mcp_server.cie_memory(action="save", title="WAV Inspector", body="Checks peak and RMS.", folder="Knowledge/Audio", tags=["audio"])
        mcp_server.cie_memory(action="save", title="Mention Note", body="We also ran WAV Inspector yesterday.", folder="Daily")

        bl = mcp_server.cie_memory(action="backlinks", title="WAV Inspector")["data"]
        self.assertEqual(len(bl["backlinks"]), 1)
        self.assertEqual(len(bl["unlinked_mentions"]), 1)

        # Update and restore version
        mcp_server.cie_memory(action="save", title="WAV Inspector", body="Overwritten body", folder="Knowledge/Audio")
        versions = mcp_server.cie_memory(action="versions", rel_path="Knowledge/Audio/WAV Inspector.md")["versions"]
        self.assertGreaterEqual(len(versions), 1)
        config.approve_tool("memory.restore")
        mcp_server.cie_memory(action="restore", rel_path="Knowledge/Audio/WAV Inspector.md", version_file=versions[0]["version_file"])
        restored_text = (Path(os.environ["CIE_VAULT"]) / "Knowledge/Audio/WAV Inspector.md").read_text(encoding="utf-8")
        self.assertIn("Checks peak and RMS.", restored_text)

        # External Obsidian edit detection
        ext = Path(os.environ["CIE_VAULT"]) / "Knowledge/Coding/ExternalEdit.md"
        ext.write_text("# External Note\n\nAdded directly in Obsidian with obsidian_unique_token.\n", encoding="utf-8")
        hits = mcp_server.cie_memory(action="search", query="obsidian_unique_token")["results"]
        self.assertEqual(len(hits), 1)

        # Export
        exp_dir = self.tmp / "exported"
        exp = vault.export_vault(str(exp_dir))
        self.assertGreaterEqual(exp["files_copied"], 4)


if __name__ == "__main__":
    unittest.main()