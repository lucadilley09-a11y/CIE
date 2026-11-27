# CIE Phase 4: Workflows and Integrations

**Status: built and verified in the sandbox (2026-11-27). The Blender part still has to be checked on the owner's PC.**

Scope (owner decision): workflows and project automation, plus the **Blender** integration only. Gradle and Roblox Studio integrations come later.

Answers from the owner:
- **Blender:** runs in the background only, with Eevee as the default renderer. It's used for all four jobs: filling CIE's 3D gaps, realistic models, texture baking, and pro renders and exports.
- **Workflows:** the owner approves once per run. A failed step retries, then the run rolls back.
- **Automation:** browser games with Hearth come first. All four ready-made workflows are included.

## What was built

| Part | Module | Capabilities |
|---|---|---|
| 4.2 Blender | `cie/blender.py`, `cie/blender_job.py` | `blender.status`, `blender.process`, `blender.render`, `blender.bake`, `blender.materials` |
| 4.1 Workflows | `cie/flows.py`, `cie approve` | `flow.list`, `flow.show`, `flow.validate`, `flow.save`, `flow.run`, `flow.resume`, `flow.history`, `flow.get`, `flow.cancel` |
| 4.3 Automation | `cie/game.py`, built-in workflows | `game.check`, `game.package`; workflows `new_browser_game`, `release_prep` |

There are now 189 capabilities (173 after Phase 3).

### Blender
- **Detection:** CIE finds Blender itself. It checks `blender_path` in the config, then PATH, then the official installer folders, Steam, and portable zips in Downloads or Desktop, and picks the newest version. `blender.status` reports the version, render engines and glTF/FBX support, and caches the result.
- **How it runs:** a hidden `blender -b --factory-startup --disable-autoexec` process runs CIE's own job script.
  - Your Blender preferences and add-ons aren't loaded.
  - Python embedded in `.blend` files never runs.
  - The job gets parameters only, never code. A test checks that the job script has no `exec`, `eval` or `subprocess` calls.
- **`blender.process`** handles what CIE's own 3D tools can't, plus realistic models:
  - booleans (union, difference, intersect, with a shape or another file), real bevels, voxel remesh, Catmull-Clark subdivision, smoothing, decimate, solidify, mirror, array, noise displacement;
  - 10 procedural materials: skin with subsurface scattering, wood, metal, gold, stone, plastic, fabric, rubber, glass, eye;
  - export to GLB, FBX (for Roblox Studio), OBJ or `.blend`.
- **`blender.render`:** Eevee (default), Cycles or Workbench, with three-light studio lighting, AgX colour and a transparent background. View presets: hero, turntable, sheet, portrait. If an engine fails in background mode, it falls back to the next one and says so.
- **`blender.bake`:** bakes normal, AO, colour and roughness maps onto the game mesh. The source is either a separate high-poly model or the same model made richer with `high_ops` and `material_ops`. The mesh is freshly unwrapped first, the maps are wired into the material, and everything is packed into the GLB.
- **Checking the results:** every Blender result goes through CIE's own mesh checks and gets a preview sheet with Blender's renders and the baked maps.
- **Without Blender:** each Blender capability returns `tool_missing` with install advice. Nothing else in CIE needs Blender.

### Workflows
- **Format:** JSON with named inputs and steps. Steps can use:
  - references: `$inputs.x`, `$item`, `$steps.<id>.data.<path>`, and `{inputs.x}` text templates;
  - `when` conditions, `foreach` (reads run 4 at a time), `retry`, `optional`;
  - quality gates: `require: pass|warn`, plus `vary: "params.seed"` so a failing result is remade with a new seed.
- **Approval once per run:**
  - If any step can change files, `flow.run` only prepares the run and lists every change it can make.
  - The owner approves outside Claude by running `cie approve <run_id>` in a terminal, which shows the plan and asks y/N. Claude cannot approve.
  - `flow.resume` then runs exactly that plan, and only the capabilities it listed can write.
  - Changing the workflow or its inputs afterwards invalidates the approval.
  - Read-only workflows run immediately.
- **Retry, then roll back:**
  - Every write is snapshotted.
  - `studio.place` and `templates.apply` snapshot the exact files they create, so a rollback deletes new files and restores replaced ones.
  - Project-wide snapshots never delete files the owner added during the run.
- **History and library:** runs are saved in `CIE_HOME/flows/runs`. `flow.history` lists them, and `cie runs` lists them in a terminal. User workflows are saved with `flow.save`.
- **Built-in workflows:**
  - `blender_polish`: Blender detail pass, then bake onto the game mesh, render, and optionally place it in a project.
  - `asset_pack`: any number of studio assets, each quality-gated with seed retries, then placed in a project.
  - `bug_hunt`: review, unused code, complexity and tests.
  - `release_prep`: review, tests, `game.check`, zip, and optionally copy the zip into the project.
  - `new_browser_game`: creates a game from the Hearth template, makes and places its assets, checks it and zips a first build. Claude then writes the gameplay.

### Browser-game checks
- **`game.check`** looks for:
  - the Hearth rules: `hearth-connect.js` first in `<head>`, guarded `Hearth.unlock`, saves in localStorage only, achievements listed in the entry;
  - missing asset files;
  - wrong letter case in paths, which works on Windows but breaks on GitHub Pages;
  - oversized files and leftover `debugger` statements.
- **`game.package`** zips the game into `CIE_HOME/studio/builds`.

## Verification
- **Sandbox (Linux, Python 3.13):** 201 passed, 6 skipped, 2 failed (the existing `trimesh` tests that can't run there). `tests/test_phase4_flows_blender.py` has 15 tests:
  - approval required, a tampered plan rejected, and runs that can't be replayed;
  - rollback removes a whole created game;
  - quality retries with a new seed;
  - foreach placement and the full `new_browser_game` run;
  - `game.check` catching each problem;
  - the Blender job protocol through a stand-in executable;
  - bad Blender ops rejected before Blender starts;
  - detection picks the newest install.
- **Real Blender:** Blender isn't installed in the sandbox. `scripts/blender_smoke.py` runs every Blender capability and the `blender_polish` workflow on the owner's PC, and copies the preview sheets to `blender_smoke_out/` for review. **Pending.**

## Known limits
- The Blender job script is written for Blender 4.2 LTS through 5.x and hasn't run on real Blender yet. The smoke test is the gate.
- Eevee needs a GPU context. Some PCs can't create one in background mode, and then CIE falls back to Cycles automatically.
- When a workflow step's arguments depend on earlier results, the approval plan shows "depends on earlier steps". Only the approved capabilities can still write.
- Gradle and Roblox Studio integrations aren't built (owner decision).
