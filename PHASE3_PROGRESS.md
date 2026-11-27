# CIE Phase 3: Creative Asset Studio

**Status: built and locally verified (2026-11-27).** No new dependencies: everything runs on numpy, scipy and Pillow, which CIE already ships. Blender, FFmpeg and AI models are optional extras, never requirements.

Owner decisions applied: 3D rigs first, then 2D animation; low-poly game assets built in, realistic work through Blender (Phase 4); pixel art first for 2D; SFX, ambience, music and editing for audio; quality judged by metrics and then a preview sheet Claude looks at.

## What was built

| Area | Module | Capabilities |
|---|---|---|
| Shared | `cie/studio.py` | `studio.list`, `studio.review`, `studio.place` (write), plus the `cie_view` MCP tool |
| 3D | `cie/mesh3d.py`, `cie/lowpoly.py` | `mesh.build`, `mesh.generate`, `mesh.edit`, `mesh.convert`, `mesh.clean`, `mesh.library` |
| Animation | `cie/rig.py` | `rig.character`, `rig.animate`, `rig.auto_rig`, `rig.object_anim` |
| 2D | `cie/pixel.py` | `pixel.palette`, `pixel.draw`, `pixel.sprite_from_3d`, `pixel.texture`, `pixel.tileset`, `pixel.sheet`, `pixel.animate`, `pixel.ui_kit`, `image.edit`, `image.vector` |
| Audio | `cie/synth.py` | `sound.sfx`, `sound.ambience`, `sound.music`, `sound.edit`, `sound.analyze` |

There are now 173 capabilities (145 after Phase 2). The older `art.*`, `audio.*` and trimesh-based `mesh.check/repair/simplify` capabilities are unchanged.

### How assets flow
1. A creative tool writes into CIE's own studio folder (`CIE_HOME/studio/<kind>/`). That is why these tools are `read` permission: they never touch a project.
2. Every output gets a sidecar `.cie.json` (how it was made, its checks) and a **preview sheet** (`CIE_HOME/studio/sheets/`).
3. Claude opens the sheet with `cie_view` and only calls the asset done when the metrics pass and it looks right.
4. `studio.place` copies the asset (plus its MTL, textures or atlas JSON) into a project. It's a write: it needs the owner's approval, snapshots first, and never overwrites without `overwrite: true`.

### 3D (3B)
- Primitives: box, rounded box, plane, wedge, cylinder/frustum, cone, UV sphere, icosphere, torus, capsule.
- Edits: extrude and inset (per connected region, cap can collapse to a point), Loop subdivision, mirror (seam faces removed), vertex-clustering decimation, taper, twist, jitter, Laplacian smoothing, weld, orient, flip, per-face materials, flat or smooth shading. Faces are selected by normal, material or height.
- UVs: box projection (not mirrored) and a chart unwrapper that flattens each chart onto its own plane and packs them without overlap. UV stretch is measured from the UV-to-3D Jacobian.
- Materials: a 29-entry PBR library (wood, planks, stone, brick, metals, glass, foliage, skin, lava...), plausibility checks (in-between metalness, too-dark metals, pure black or white albedo), and baked atlas textures with wood, planks, brick, stone and noise patterns.
- Validation: holes, non-manifold edges, inconsistent winding, inside-out normals, degenerate, sliver, duplicate faces, loose vertices, scale, triangle budgets (Roblox 20k limit), UV stretch. Parts that only touch (a rail meeting a post) aren't reported as non-manifold.
- I/O: OBJ+MTL, binary/ASCII STL, glTF and GLB (embedded textures, skins, animations), read and write.
- Preview: CIE's own renderer draws 4 views, a wireframe and the back. Back faces show pink and open edges red, so holes and flipped faces are visible.
- 12 parametric generators. Each returns its editable `mesh.build` spec.

### Animation (3C)
- R15-named humanoid (16 bones) in blocky or rounded style, with slim, normal, broad or chibi build, colours, hair and a hat option. Parts are bound rigidly, as Roblox characters are.
- Clips:
  - **Presets:** idle, walk, run, jump, wave, attack.
  - **Locomotion:** walk, run and blended jogs use two-bone leg IK, solved in the pelvis frame, so the planted foot moves at exactly the ground speed. The clip reports that speed, plus the matching Roblox WalkSpeed.
  - **Jump:** root motion at real gravity, with anticipation, push-off, landing and recovery that hand speed from one phase to the next.
  - **Custom keyframes:** smooth Catmull-Rom by default, or per-key easing (linear, in, out, inout, step, back, elastic) for deliberate snaps.
  - **Clip tools:** retime, blend (walk/run blends are regenerated with blended parameters so the footwork stays correct), mirror left/right.
- `rig.auto_rig` fits the skeleton to any standing mesh and skins it with distance weights.
- `rig.object_anim` animates props: spin, bob, hinge, pulse, or custom position, rotation and scale keys.
- Metrics:
  - loop seam: pose, and speed compared with how fast speed changes inside the clip;
  - pops: sudden speed changes;
  - foot sliding: measured on the planted foot;
  - ground penetration.
- Export: one GLB with skin and every clip, a GIF per clip, a `.rig.json` for later edits, and a preview sheet with frames and foot-trajectory plots.

### 2D (3A)
- **Colour:** OKLab/OKLCH, hue-shifted pixel ramps (cool shadows, warm highlights, about 20° at the ends), and six harmony schemes with neutrals. Exports PNG, Lospec `.hex` and GIMP/Aseprite `.gpl`.
- **Drawing:** layered canvas with these ops: pixel, Bresenham line, rect, ellipse, polygon, scanline flood fill, ordered-dither gradient, outline, selective outline, auto-shade from a ramp, noise, symmetry, shift, flip, rotate, copy, stamp, replace. Palette colours can be used by index.
- **8-direction sprites:** a CIE character or any model is rendered at 8 yaws × N animation frames, area-downsampled, quantised to a palette built from its materials and outlined. Every frame is the same character, consistent by construction. Writes the sheet, Aseprite-style JSON with frame tags, a GIF and the palette.
- **Textures and tiles:**
  - 11 seamless textures: grass, dirt, sand, stone, water, snow, lava, wood, brick, cobble, metal.
  - 16-tile dual-grid terrain transitions. A periodic noise field makes every shared edge match. Comes with mapping JSON and a demo map.
- **Sheets and animation:** bottom-aligned packing; effects bob, squash, flash, blink, sway, shake and spin, or per-frame draw ops; GIF; onion-skin previews.
- **UI kit:** 9-slice panel, buttons (normal, hover, pressed), slot and bar, with slice metadata for Godot, Roblox and CSS.
- **Editing:** resize, crop, trim, pad, flip, rotate, OKLCH adjust, blur, sharpen, background removal (OKLab flood from the border with edge colour decontamination), quantise, pixelate, outline, drop shadow, glow, compose, circle mask.
- **Vector:** rect, circle, ellipse, polygon, path (M/L/Z), line and text, with flat or linear-gradient fills and strokes. Writes an SVG and an anti-aliased PNG.
- **Checks:** palette size and off-palette colours, stray and orphan pixels, soft alpha in pixel art, missing anti-aliasing on large art, JPEG block artefacts, sprite baseline jitter across cells, frames touching cell edges, and tiling seams (the wrap-around edge compared with every interior neighbour pair).

### Audio (3D)
- **Synthesis:**
  - **Oscillators:** sine, triangle, and PolyBLEP band-limited square and saw (no aliasing fizz); white, pink and brown noise.
  - **Envelopes:** ADSR and exponential decay.
  - **Filters:** RBJ biquads (lowpass, highpass, bandpass, peak, shelves), static or swept without clicks.
  - **Effects:** distortion, bit-crush, delay, and a fast Schroeder reverb.
- **SFX:** 20 kinds, each seeded so `variations: 5` gives five different takes. All are trimmed, faded, normalised to −16 LUFS (UI −19) and limited under −1 dBTP:
  coin, jump, laser, shoot, explosion, hit, hurt, powerup, pickup, blip, ui_click, ui_confirm, ui_back, error, magic, whoosh, door, footstep_grass, footstep_stone, footstep_wood.
- **Ambience:** wind, rain, cave, forest, ocean, fire and hum. Stereo, closed into a seamless loop with an equal-power crossfade, at −24 LUFS.
- **Music:**
  - 7 moods × 4 styles (chiptune, ambient, lofi, battle), any key.
  - Parts: bass, chords or arpeggio, a seeded motif melody that lands on chord tones, and drums.
  - The reverb and release tail is folded back to the start so the loop is seamless.
  - Also writes a type-1 MIDI file to edit in any DAW.
- **Editing:** 27 ops, including trim, fades (linear, equal-power, exponential), gain automation, LUFS or peak normalise, 3-band EQ, pitch with or without speed (phase vocoder), time-stretch, echo, reverb, spectral-gate denoise, make-loop, split on silence (parts are faded), concat with crossfade, mix, resample, and 24-bit WAV.
- **Analysis:**
  - Levels: integrated LUFS (BS.1770 K-weighting, checked against the reference tone), true peak, crest, DC.
  - Faults: clipping, isolated clicks (square-wave edges are not counted), clicks at the start or end, silence at the start or end, gaps inside the sound.
  - Loops: seam jump, and the level change at the loop point compared with level changes inside the clip.
  - Targets: loudness ranges per kind.
  - Preview: waveform and spectrogram.
- **OGG and MP3** export uses FFmpeg when it's installed; otherwise the WAV is delivered and the result says so.

## Bugs the quality checks caught during the build
- **Walk cycle sliding about 9 cm per step.** Leg IK ignored pelvis yaw, so the foot drifted sideways. It's now solved in the pelvis frame with a roll toward the target, and sliding measures 0.0 cm.
- **Hitch at touchdown.** The swing curve now leaves and lands at the planted foot's speed (Hermite), and the knee never fully locks.
- **Pops in the jump and attack.** Phases now pass on their speed, and custom keys default to Catmull-Rom.
- **Validation against itself:** parts that touch were flagged non-manifold (fixed with shell separation); long thin cylinder quads were flagged as slivers; extrude left loose vertices.
- **Colour:** OKLab returned linear instead of sRGB, which made skin brown. Hue shifts were too strong.
- **Seam check:** it used a mean-difference ratio that false-alarmed on sparse details. It's now judged against interior neighbour pairs.
- **Click detector:** square waves counted as clicks. It now looks for isolated jumps.
- **Music checks:** DC offset from pulse waves, and loop-level warnings that compared musical decay with the seam.
- **`studio.place`:** placing into a new folder name created a file called `models`.

## Verification (Linux sandbox, Python 3.13)
- `python -m pytest -q`: 186 passed, 6 skipped, 2 failed. The 2 failures are the existing `trimesh` tests, which can't run in this sandbox.
- `tests/test_phase3_studio.py` has 25 tests. They drive every capability through the registry and check quality metrics, not only that files exist:
  - every generator, preset and SFX passes its own checks;
  - files round-trip;
  - tile edges match;
  - LUFS matches the BS.1770 reference tone;
  - studio tools never write into a project.
- Preview sheets for the crate, all 12 generators, the 7-clip character, the 8-direction sprite, all textures, the tileset and the music were inspected by eye.

## Known limits
- No boolean CSG or true edge bevels on arbitrary meshes; rounded boxes and inset cover common cases. Blender (Phase 4) adds the rest.
- Characters are blocky or rounded game characters, not realistic humans. Auto-rigging expects a standing figure facing +Z.
- Pixel sprites at 32 px lose sub-pixel details such as eyes. Use 48–64 px, or touch up with `pixel.draw`.
- Music is pattern-based and good for loops and placeholders, not a replacement for a composer. The MIDI file is there for hand edits.
- Background removal assumes a fairly uniform background touching the image border.
- WAV in and out natively; OGG and MP3 need FFmpeg.
