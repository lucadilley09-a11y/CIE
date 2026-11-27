import json
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from cie import config, creative, external, registry, storage

SOUNDFONT = next((p for p in [Path("/usr/share/sounds/sf2/TimGM6mb.sf2"), Path("/usr/share/sounds/sf2/FluidR3_GM.sf2")] if p.exists()), None)


def _write_midi(path: Path) -> None:
    """Tiny one-track MIDI: middle C for half a second."""
    track = bytes([0x00, 0x90, 60, 100, 0x83, 0x60, 0x80, 60, 0, 0x00, 0xFF, 0x2F, 0x00])
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + b"MTrk" + struct.pack(">I", len(track)) + track
    path.write_bytes(data)


class CreativeToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_cr_"))
        os.environ["CIE_HOME"] = str(self.tmp / "home")
        storage._DEFAULT_STORAGE = None
        registry._REGISTRY = None
        self.proj = self.tmp / "game"
        self.proj.mkdir()
        config.add_project(str(self.proj), "game")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_new_capabilities_registered(self) -> None:
        ids = {c["id"] for c in registry.get_registry().discover()}
        for cid in ["lint.gdlint", "lint.gdformat", "coding.coverage", "coding.profile", "sandbox.run",
                    "github.list", "github.view", "github.pr_diff", "art.blender_export", "art.blender_render",
                    "mesh.check", "mesh.repair", "mesh.simplify", "gltf.validate", "art.svg_export",
                    "audio.midi_render", "audio.puredata_run", "audio.supercollider_run"]:
            self.assertIn(cid, ids)

    def test_new_tools_show_in_availability_report(self) -> None:
        report = external.tools_report()["data"]["installed"]
        for name in ["gdlint", "fluidsynth", "sclang", "pd", "docker", "gh"]:
            self.assertIn(name, report)

    # ---------- Coding ----------

    @unittest.skipUnless(shutil.which("gdlint"), "gdtoolkit not installed")
    def test_gdlint_and_gdformat(self) -> None:
        (self.proj / "player.gd").write_text("extends Node\nfunc BadName():\n\tpass\n", encoding="utf-8")
        res = creative.lint_gdlint("player.gd")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(any(f["rule"] == "function-name" for f in res["data"]["findings"]))
        (self.proj / "ugly.gd").write_text("extends Node\nvar x=1\n", encoding="utf-8")
        check = creative.lint_gdformat("ugly.gd")
        self.assertEqual(check["summary"], "Files need formatting.")
        creative.lint_gdformat("ugly.gd", apply=True)
        self.assertEqual(creative.lint_gdformat("ugly.gd")["summary"], "Formatting is clean.")

    @unittest.skipUnless(shutil.which("coverage"), "coverage not installed")
    def test_coverage_finds_untested_function(self) -> None:
        (self.proj / "mathy.py").write_text(
            "def used(x):\n    return x + 1\n\ndef unused(x):\n    return x * 2\n", encoding="utf-8")
        (self.proj / "test_mathy.py").write_text(
            "import unittest\nimport mathy\n\nclass T(unittest.TestCase):\n"
            "    def test_used(self):\n        self.assertEqual(mathy.used(1), 2)\n", encoding="utf-8")
        res = creative.coding_coverage("unittest")
        self.assertEqual(res["status"], "ok", res)
        self.assertTrue(res["data"]["tests_passed"])
        mathy = next(f for f in res["data"]["least_covered"] if f["file"].endswith("mathy.py") and "test_" not in f["file"])
        self.assertIn(5, mathy["missing_lines"])

    def test_coverage_refuses_non_python_allowlist_entry(self) -> None:
        with self.assertRaises(PermissionError):
            creative.coding_coverage("ruff")

    @unittest.skipUnless(shutil.which("py-spy"), "py-spy not installed")
    def test_profile_finds_hot_function(self) -> None:
        (self.proj / "slow.py").write_text(
            "def hot():\n    return sum(i * i for i in range(2_000_000))\n\nfor _ in range(4):\n    hot()\n", encoding="utf-8")
        res = creative.coding_profile("slow.py", duration_sec=30, rate=200)
        if res["status"] != "ok":
            self.skipTest(f"py-spy cannot sample here: {res['summary']}")
        frames = " ".join(f["frame"] for f in res["data"]["cumulative"])
        self.assertIn("hot", frames)

    def test_docker_command_is_locked_down(self) -> None:
        argv = creative.sandbox_docker_argv("docker", self.proj, "main.py", "python:3.12-slim", 30)
        joined = " ".join(argv)
        for flag in ["--network none", "--read-only", "--cap-drop ALL", "--pull never", ":/work:ro", "no-new-privileges"]:
            self.assertIn(flag, joined)

    def test_docker_rejects_bad_image_and_outside_script(self) -> None:
        if not shutil.which("docker"):
            self.skipTest("docker not installed")
        (self.proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            creative.sandbox_run("main.py", image="evil; rm -rf /")
        with self.assertRaises(PermissionError):
            creative.sandbox_run("../outside.py")

    def test_github_input_validation(self) -> None:
        with self.assertRaises(ValueError):
            creative.github_list(kind="repo")
        with self.assertRaises(ValueError):
            creative.github_list(kind="issue", state="merged")
        with self.assertRaises(ValueError):
            creative.github_view("pr", 0, repo="owner/name")
        if shutil.which("gh"):
            with self.assertRaises(ValueError):
                creative.github_list(repo="not a repo; ls")

    # ---------- 3D and art ----------

    def test_blender_missing_is_graceful(self) -> None:
        if creative.detect_blender():
            self.skipTest("Blender installed; missing-path not exercised")
        (self.proj / "scene.blend").write_bytes(b"BLENDER")
        self.assertEqual(creative.blender_export("scene.blend", "scene.glb")["status"], "tool_missing")
        self.assertEqual(creative.blender_render("scene.blend", "shot.png")["status"], "tool_missing")

    def test_mesh_check_repair_simplify(self) -> None:
        import trimesh
        box = trimesh.creation.box()
        open_box = box.copy()
        open_box.update_faces([i for i in range(len(open_box.faces)) if i != 0])  # punch a hole
        open_box.export(str(self.proj / "holey.obj"))
        bad = creative.mesh_check("holey.obj")
        self.assertFalse(bad["data"]["watertight"])
        self.assertGreater(bad["data"]["open_edges"], 0)

        fixed = creative.mesh_repair("holey.obj", "fixed.obj")
        self.assertEqual(fixed["status"], "ok")
        self.assertTrue(fixed["data"]["after"]["watertight"])

        trimesh.creation.icosphere(subdivisions=4).export(str(self.proj / "sphere.stl"))
        try:
            import fast_simplification  # noqa: F401
        except ImportError:
            self.skipTest("Optional fast-simplification dependency is not installed")
        low = creative.mesh_simplify("sphere.stl", "sphere_low.stl", ratio=0.25)
        self.assertEqual(low["status"], "ok", low)
        self.assertLess(low["data"]["faces_after"], low["data"]["faces_before"] * 0.4)

        with self.assertRaises(ValueError):
            creative.mesh_repair("holey.obj", "holey.obj")

    def test_gltf_validate_good_and_broken(self) -> None:
        import trimesh
        trimesh.creation.box().export(str(self.proj / "box.glb"))
        good = creative.gltf_validate("box.glb")
        self.assertEqual(good["status"], "ok")
        if good["data"]["validator"] == "basic":
            self.assertEqual(good["data"]["issues"], [])

        doc = {"asset": {"version": "1.0"}, "buffers": [{"uri": "missing.bin", "byteLength": 10}],
               "bufferViews": [{"buffer": 3, "byteLength": 4}],
               "meshes": [{"primitives": [{"attributes": {"NORMAL": 9}}]}]}
        (self.proj / "broken.gltf").write_text(json.dumps(doc), encoding="utf-8")
        broken = creative.gltf_validate("broken.gltf")
        if broken["data"]["validator"] == "basic":
            text = " ".join(broken["data"]["issues"])
            for needle in ["asset.version", "missing.bin", "missing buffer 3", "no POSITION", "missing accessor 9"]:
                self.assertIn(needle, text)

    def test_svg_export_exact_size(self) -> None:
        try:
            import cairosvg  # noqa: F401
        except ImportError:
            if not shutil.which("inkscape"):
                self.skipTest("no SVG renderer installed")
        (self.proj / "icon.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>',
            encoding="utf-8")
        res = creative.svg_export("icon.svg", "icon.png", width=64, height=64)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(res["data"]["size"], [64, 64])

    # ---------- Sound ----------

    @unittest.skipUnless(shutil.which("fluidsynth") and SOUNDFONT, "fluidsynth or soundfont not installed")
    def test_midi_render_produces_audio(self) -> None:
        import numpy as np
        import soundfile as sf
        _write_midi(self.proj / "theme.mid")
        shutil.copy(SOUNDFONT, self.proj / "gm.sf2")
        res = creative.audio_midi_render("theme.mid", "gm.sf2", "theme.wav", sample_rate=44100)
        self.assertEqual(res["status"], "ok", res)
        data, rate = sf.read(str(self.proj / "theme.wav"))
        self.assertEqual(rate, 44100)
        self.assertGreater(float(np.max(np.abs(data))), 0.001)

    def test_midi_render_validates_inputs(self) -> None:
        _write_midi(self.proj / "theme.mid")
        (self.proj / "gm.sf2").write_bytes(b"x")
        with self.assertRaises(ValueError):
            creative.audio_midi_render("theme.mid", "gm.sf2", "theme.wav", sample_rate=12345)
        with self.assertRaises(ValueError):
            creative.audio_midi_render("theme.mid", "gm.sf2", "theme.exe")

    def test_puredata_and_supercollider_validate(self) -> None:
        (self.proj / "patch.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            creative.audio_puredata_run("patch.txt")
        with self.assertRaises(ValueError):
            creative.audio_supercollider_run("patch.txt")
        (self.proj / "synth.pd").write_text("#N canvas 0 0 100 100 10;\n", encoding="utf-8")
        if not shutil.which("pd"):
            self.assertEqual(creative.audio_puredata_run("synth.pd")["status"], "tool_missing")


class SoxRealRunTests(unittest.TestCase):
    """v1.1 sox wrapper, now that sox is available to test against."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_sox_"))
        os.environ["CIE_HOME"] = str(self.tmp / "home")
        registry._REGISTRY = None
        self.proj = self.tmp / "game"
        self.proj.mkdir()
        config.add_project(str(self.proj), "game")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(shutil.which("sox"), "sox not installed")
    def test_sox_pitch_and_reverb(self) -> None:
        import numpy as np
        import soundfile as sf
        t = np.linspace(0, 0.5, 22050, endpoint=False)
        sf.write(str(self.proj / "beep.wav"), 0.4 * np.sin(2 * np.pi * 440 * t), 44100)
        res = external.audio_sox_effect("beep.wav", "beep_up.wav", "pitch", ["700"])
        self.assertEqual(res["status"], "ok", res)
        res = external.audio_sox_effect("beep.wav", "beep_verb.wav", "reverb", ["50"])
        self.assertEqual(res["status"], "ok", res)
        self.assertTrue((self.proj / "beep_verb.wav").exists())


if __name__ == "__main__":
    unittest.main()