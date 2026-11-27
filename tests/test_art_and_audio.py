import os
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import mcp_server
from cie import config, jobs, registry, storage


class ArtAndAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cie_media_"))
        os.environ["CIE_HOME"] = str(self.tmp / "cie_home")
        os.environ["CIE_VAULT"] = str(self.tmp / "CIE-Vault")
        storage._DEFAULT_STORAGE = None
        jobs._JOB_MANAGER = None
        registry._REGISTRY = None
        config.save_config({**config.load_config(), "autonomy_level": "autonomous"})

        self.proj = self.tmp / "media_proj"
        self.proj.mkdir()
        config.add_project(str(self.proj), "media")

        # Create 64x64 RGBA sprite sheet (2x2 of 32x32) with 1 empty frame
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        for x in range(32):
            for y in range(32):
                im.putpixel((x, y), (255, 0, 0, 255))
                im.putpixel((x + 32, y), (0, 255, 0, 255))
                im.putpixel((x, y + 32), (0, 0, 255, 255))
        self.sprite_path = self.proj / "sheet.png"
        im.save(self.sprite_path)

        self.obj_path = self.proj / "cube.obj"
        self.obj_path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 3\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_art_and_3d_tools(self) -> None:
        info = mcp_server.cie_run("art.image_inspector", {"path": str(self.sprite_path)})["data"]
        self.assertEqual(info["width"], 64)
        self.assertTrue(info["has_alpha"])

        spr = mcp_server.cie_run("art.sprite_validate", {"path": str(self.sprite_path), "cell_width": 32, "cell_height": 32})["data"]
        self.assertTrue(spr["grid_aligned"])
        self.assertEqual(spr["total_frames"], 4)
        self.assertEqual(spr["empty_frames"], 1)

        tile = mcp_server.cie_run("art.tile_check", {"path": str(self.sprite_path)})["data"]
        self.assertTrue(tile["power_of_two"])

        pal = mcp_server.cie_run("art.palette_analyzer", {"path": str(self.sprite_path), "max_colors": 4})["data"]
        self.assertGreaterEqual(len(pal["dominant_colors"]), 1)

        obj = mcp_server.cie_run("art.inspect_3d", {"path": str(self.obj_path)})["data"]
        self.assertEqual(obj["vertices"], 3)
        self.assertEqual(obj["faces"], 1)

    def test_audio_sfx_inspect_fft_and_optimize(self) -> None:
        wav_path = self.proj / "beep.wav"
        sfx = mcp_server.cie_run(
            "audio.procedural_sfx",
            {"output_path": str(wav_path), "waveform": "sine", "freq_hz": 440.0, "duration_sec": 0.5},
        )
        self.assertEqual(sfx["status"], "ok")

        insp = mcp_server.cie_run("audio.inspector", {"path": str(wav_path)})["data"]
        self.assertEqual(insp["sample_rate"], 44100)
        self.assertAlmostEqual(insp["duration_sec"], 0.5, places=2)

        fft = mcp_server.cie_run("audio.frequency", {"path": str(wav_path), "top_k": 3})["data"]
        self.assertAlmostEqual(fft["dominant_frequencies_hz"][0]["hz"], 440.0, delta=15.0)

        qa = mcp_server.cie_run("audio.silence_clipping", {"path": str(wav_path)})["data"]
        self.assertFalse(qa["has_clipping"])

        opt_path = self.proj / "beep_norm.wav"
        opt = mcp_server.cie_run("audio.optimize", {"path": str(wav_path), "output_path": str(opt_path), "target_peak_dbfs": -1.0})
        self.assertEqual(opt["status"], "ok")
        self.assertTrue(opt_path.exists())


if __name__ == "__main__":
    unittest.main()