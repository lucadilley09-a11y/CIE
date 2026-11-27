import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cie import config, external, registry, storage


class ExternalToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_ext_"))
        os.environ["CIE_HOME"] = str(self.tmp / "home")
        storage._DEFAULT_STORAGE = None
        registry._REGISTRY = None
        self.proj = self.tmp / "game"
        self.proj.mkdir()
        (self.proj / "ok.py").write_text("x = 1\n", encoding="utf-8")
        config.add_project(str(self.proj), "game")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_capability_is_registered(self) -> None:
        ids = {c["id"] for c in registry.get_registry().discover()}
        for cid in ["git.log", "git.diff", "lint.ruff", "security.bandit", "engine.godot_check",
                    "art.aseprite_export", "art.atlas_pack", "audio.ffmpeg_convert", "audio.lufs",
                    "search.semantic_query", "core.tools_available"]:
            self.assertIn(cid, ids)

    def test_missing_tool_reports_install_hint_not_crash(self) -> None:
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.tmp)  # no tools reachable
        try:
            res = external.security_gitleaks(".")
            self.assertEqual(res["status"], "tool_missing")
            self.assertIn("gitleaks", res["summary"])
        finally:
            os.environ["PATH"] = old

    def test_unknown_tool_name_is_refused(self) -> None:
        with self.assertRaises(PermissionError):
            external.find_tool("rm")

    def test_sox_rejects_non_whitelisted_effect_and_shell_args(self) -> None:
        (self.proj / "a.wav").write_bytes(b"RIFF")
        with self.assertRaises(ValueError):
            external.audio_sox_effect("a.wav", "b.wav", "exec")
        with self.assertRaises(ValueError):
            external.audio_sox_effect("a.wav", "b.wav", "gain", ["-n; rm -rf /"])

    def test_output_must_stay_in_project(self) -> None:
        with self.assertRaises(PermissionError):
            external._safe_out("../escape.png")

    def test_atlas_pack_produces_frames(self) -> None:
        from PIL import Image
        for name, size in [("a.png", (8, 8)), ("b.png", (4, 12)), ("c.png", (16, 4))]:
            Image.new("RGBA", size, (255, 0, 0, 255)).save(self.proj / name)
        res = external.art_atlas_pack(["a.png", "b.png", "c.png"], "atlas.png", "atlas.json", padding=1, max_width=64)
        self.assertEqual(res["status"], "ok")
        meta = json.loads((self.proj / "atlas.json").read_text(encoding="utf-8"))
        self.assertEqual(len(meta["frames"]), 3)
        # No two frames overlap.
        boxes = [(f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"]) for f in meta["frames"]]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])

    def test_lufs_measures_a_tone(self) -> None:
        import numpy as np
        import soundfile as sf
        t = np.linspace(0, 1.0, 48000, endpoint=False)
        sf.write(str(self.proj / "tone.wav"), 0.5 * np.sin(2 * np.pi * 440 * t), 48000)
        res = external.audio_lufs("tone.wav")
        self.assertEqual(res["status"], "ok")
        self.assertLess(res["data"]["integrated_lufs"], 0)

    def test_semantic_search_without_model_is_graceful(self) -> None:
        res = external.search_semantic_index()
        self.assertEqual(res["status"], "model_missing")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_git_log_in_fresh_repo(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        subprocess.run(["git", "-C", str(self.proj), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "first"], check=True)
        res = external.git_log(count=5)
        self.assertEqual(res["status"], "ok")
        self.assertIn("first", res["data"]["stdout"])

    @unittest.skipUnless(shutil.which("ruff"), "ruff not installed")
    def test_ruff_reports_findings(self) -> None:
        (self.proj / "bad.py").write_text("import os\n", encoding="utf-8")
        res = external.lint_ruff(".")
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["data"]["total"], 1)


if __name__ == "__main__":
    unittest.main()