# CIE development decisions (owner-approved)

Later sessions follow these unless the owner changes them. Newest first.

## Phases 2–6 (2026-11-27)

| Area | Decision |
|---|---|
| Build order | Phase 2 coding, then Phase 3 creative studio, then 4 integrations, 5 polish, 6 release. Owner installs once at the end |
| Platform | Windows only (owner's PC). CI, wheels, and bundles run on `windows-2022` |
| Code languages | Luau (Roblox), JavaScript/HTML (browser games, Hearth), Java/Kotlin (Minecraft mods). Python stays supported |
| Code fixes | CIE shows the exact diff; it applies only after approval, with an automatic snapshot |
| 2D | Pixel art first: sprites, tilesets, sheets, palettes |
| 3D | Low-poly game assets built into CIE; realistic work goes through Blender when installed |
| Animation | 3D rigs first (Roblox-style skeletal), then 2D sprite animation |
| Audio | Game SFX, ambience and loops, simple music, and editing tools, all synthesized locally |
| Creative quality | Objective metrics, then a rendered preview sheet that Claude inspects before calling an asset done |
| Integrations | Blender, Gradle (Minecraft mod builds), Roblox Studio. All optional |
| Interface | Claude only, plus a small status page. No full dashboard |
| Final install | Run CIE from the owner's folder, so updates only need a Claude restart |
| Approvals | Autonomy stays `assist`; testing happens in Claude's sandbox |

## Phase 3 (2026-11-27)

See `PHASE3_PROGRESS.md`. Creative tools write only into CIE's studio folder, so they run as `read` without approval prompts; copying an asset into a project (`studio.place`) is a `write` that needs approval and snapshots first. Quality gate: metrics first, then a preview sheet Claude views through the new `cie_view` tool. No new dependencies (numpy, scipy, Pillow only); FFmpeg optional for OGG/MP3. Characters face +Z (glTF), metres, Y up.

## Phase 1 (2026-11-27)

See `PHASE1_PROGRESS.md`. Snapshots before every write; 30-day retention plus named snapshots; warn at 5 GB, always ask before cleaning; lossless compression only; git kept separate; project duplicate scanning is report-only.

## Phase 0 (2026-11-27)

Online-first install, local-first runtime, near-zero setup, no paid services. Claude only overrides or deletes ChatGPT's changes when there is a real reason, and says why.
