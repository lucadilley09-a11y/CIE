"""Real-Blender smoke test for CIE's Blender integration (Phase 4).

Run on a PC with Blender installed:
    python scripts/blender_smoke.py

It makes a crate with CIE, then runs it through Blender: bevel + boolean cut + wood material +
Eevee render, a Cycles turntable, a texture bake, and the blender_polish workflow. Preview
sheets and a summary are copied to ./blender_smoke_out/ (git-ignored) so they can be reviewed.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cie import registry  # noqa: E402

OUT = ROOT / "blender_smoke_out"


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    summary: dict = {}

    def step(name: str, cap: str, args: dict) -> dict:
        t0 = time.time()
        try:
            res = reg.run(cap, args)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            res = {"status": "crash", "summary": f"{exc}\n{traceback.format_exc()[-800:]}", "data": {}}
        data = res.get("data") or {}
        entry = {"status": res["status"], "summary": str(res.get("summary"))[:600], "seconds": round(time.time() - t0, 1),
                 "verdict": data.get("verdict") if isinstance(data, dict) else None}
        if isinstance(data, dict):
            if res["status"] == "ok" and data.get("status") in ("failed", "rolled_back"):
                entry["status"] = data["status"]
            for k in ("version", "path", "engines", "gltf", "fbx", "stats", "log", "issues", "steps"):
                if k in data:
                    entry[k] = data[k]
            prev = data.get("preview")
            if prev and Path(prev).exists():
                shutil.copy2(prev, OUT / f"{name}.png")
                entry["preview_copy"] = f"blender_smoke_out/{name}.png"
        summary[name] = entry
        print(f"{name:10s} {entry['status']:<12} {entry['seconds']:>6}s  {entry['summary'][:140]}")
        return data if isinstance(data, dict) else {}

    st = step("status", "blender.status", {"refresh": True})
    if not st.get("available"):
        print("\nBlender was NOT found, so nothing was tested (this is not a pass).")
        print(st.get("message", ""))
        (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        return 2
    else:
        crate = step("crate", "mesh.generate", {"kind": "crate", "params": {"seed": 1}})
        src = crate["files"][0]
        step("process", "blender.process", {"source": src, "name": "smoke_crate", "formats": ["glb", "fbx"], "ops": [
            {"op": "bevel", "width": 0.012, "segments": 3},
            {"op": "boolean", "operation": "difference", "with": {"shape": "sphere", "size": 0.35, "position": [0, 1.0, 0.47]}},
            {"op": "shade_smooth", "angle": 35}, {"op": "material", "preset": "wood"}], "engine": "eevee", "size": 384})
        step("cycles", "blender.render", {"source": src, "name": "smoke_turntable", "engine": "cycles", "views": "turntable",
                                         "size": 256, "samples": 16})
        step("bake", "blender.bake", {"source": src, "name": "smoke_baked", "size": 512, "maps": ["normal", "ao", "color"],
                                     "high_ops": [{"op": "bevel", "width": 0.015, "segments": 3}, {"op": "shade_smooth", "angle": 35}],
                                     "render_size": 384})
        barrel = step("barrel", "mesh.generate", {"kind": "barrel"})
        # organic detail: subdivide + noise on a rock, baked back onto the low-poly rock
        rock = step("rock", "mesh.generate", {"kind": "rock", "params": {"seed": 3}})
        step("rock_bake", "blender.bake", {"source": rock["files"][0], "name": "smoke_rock", "size": 512, "maps": ["normal", "ao", "color"],
                                          "high_ops": [{"op": "subdivide", "levels": 2}, {"op": "displace", "strength": 0.04, "scale": 0.15}],
                                          "material_ops": [{"op": "material", "preset": "stone"}], "render_size": 384})
        step("polish", "flow.run", {"workflow": "blender_polish", "inputs": {"source": barrel["files"][0], "material": "wood",
                                                                            "texture_size": 512}})
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    bad = [k for k, v in summary.items() if v["status"] != "ok" or v.get("verdict") == "fail"]
    print("\nAll Blender steps passed." if not bad else f"\nProblems in: {', '.join(bad)} (details in blender_smoke_out/summary.json)")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
