import os
import shutil
import tempfile
import unittest
from pathlib import Path

import mcp_server
from cie import config, jobs, registry, storage


class EfficiencyAndCodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_code_"))
        os.environ["CIE_HOME"] = str(self.tmp / "cie_home")
        os.environ["CIE_VAULT"] = str(self.tmp / "CIE-Vault")
        storage._DEFAULT_STORAGE = None
        jobs._JOB_MANAGER = None
        registry._REGISTRY = None

        self.proj = self.tmp / "sample_proj"
        self.proj.mkdir()
        (self.proj / "app.py").write_text(
            "import json\n\nclass Engine:\n    def run(self, x):\n        return x * 2\n\ndef helper():\n    return 1\n",
            encoding="utf-8",
        )
        (self.proj / "broken.py").write_text("def broken(:\n  pass\n", encoding="utf-8")
        (self.proj / "test_app.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
        config.add_project(str(self.proj), "sample")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_efficiency_pipeline(self) -> None:
        idx1 = mcp_server.cie_run("eff.incremental_index", {"project_id": "sample"})["data"]
        self.assertGreaterEqual(idx1["indexed"], 3)
        idx2 = mcp_server.cie_run("eff.incremental_index", {"project_id": "sample"})["data"]
        self.assertEqual(idx2["indexed"], 0)
        self.assertGreaterEqual(idx2["skipped_unchanged"], 3)

        shrunk = mcp_server.cie_run("eff.shrink_file", {"path": str(self.proj / "app.py")})["data"]
        names = [s["name"] for s in shrunk["symbols"]]
        self.assertIn("Engine", names)
        self.assertIn("run", names)

        hits = mcp_server.cie_run("eff.search_files", {"query": "Engine", "project_id": "sample"})["data"]
        self.assertGreaterEqual(hits["total"], 1)

        deduped = mcp_server.cie_run("eff.dedupe_log", {"log_text": "2026-11-25T10:00:00Z ERR timeout\n2026-11-25T10:00:01Z ERR timeout\nOK\n"})["data"]
        self.assertEqual(deduped["groups"][0]["count"], 2)

    def test_coding_brain_dependencies_and_smart_tests(self) -> None:
        brain = mcp_server.cie_run("coding.brain", {"project_id": "sample"})["data"]
        self.assertEqual(brain["projects"][0]["project_id"], "sample")
        # Syntax error in broken.py must not crash indexing
        syms = mcp_server.cie_run("coding.symbol_search", {"query": "helper", "project_id": "sample"})["data"]
        self.assertEqual(syms["total"], 1)

        deps = mcp_server.cie_run("coding.dependencies", {"project_id": "sample"})["data"]
        self.assertTrue(any(e["target"] == "json" and e["confidence"] == "exact" for e in deps["edges"]))

        smart = mcp_server.cie_run("coding.smart_tests", {"changed_files": ["app.py"], "project_id": "sample"})["data"]
        self.assertEqual(len(smart["selected_tests"]), 1)
        self.assertIn("coverage_uncertainty", smart)


if __name__ == "__main__":
    unittest.main()