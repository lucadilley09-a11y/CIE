# CIE Phase 1: Intelligent Core and Storage

**Status: built and locally verified, awaiting owner review (2026-11-27).** Built in the owner's chosen order: 1.3 storage, 1.4 knowledge, 1.1 orchestration, 1.2 routing, 1.5 templates. No new dependencies: everything uses the Python standard library plus Pillow, already a Tier 1 dependency.

## Owner decisions applied

| Setting | Decision |
|---|---|
| Automatic snapshots | Before every write or exec capability that touches a registered project |
| Retention | 30 days, plus named or pinned snapshots kept forever |
| Disk limit | Warn at 5 GB (`storage.limit_gb`); cleaning always asks first |
| Large files | Deduplicated by SHA-256 and compressed losslessly (zlib, kept only when it saves at least 5%) |
| Rollback | Preview, then a safety snapshot of the current state, then restore |
| Asset compression | Lossless only; PNG pixels verified identical before replacing |
| Git | Separate; git history is never touched |
| Project scanning | Duplicate report only; changes nothing |

## What was built

| Part | Module | Capabilities |
|---|---|---|
| 1.3 Storage | `cie/snapstore.py` | `storage.snapshot`, `storage.snapshots`, `storage.restore_preview`, `storage.restore`, `storage.name_snapshot`, `storage.compare`, `storage.report`, `storage.prune`, `storage.prune_apply`, `storage.duplicates`, `storage.optimize_png`, `storage.archive`, `storage.archive_restore` |
| 1.4 Knowledge | `cie/knowledge.py` | `knowledge.summary`, `knowledge.assets`, `knowledge.record`, `knowledge.issue_status`, `knowledge.query`, `knowledge.indexer_status`, `knowledge.reindex_now` |
| 1.1 Orchestration | `cie/orchestrate.py` | `orchestrate.plan`, `orchestrate.run` |
| 1.2 Routing | `cie/routing.py` | `routing.recommend`, `routing.status`, `routing.set_mode` |
| 1.5 Templates | `cie/templates.py` | `templates.list`, `templates.show`, `templates.validate`, `templates.apply`, `templates.save` |

There are now 126 registered capabilities (96 before Phase 1).

### Storage details
- Each unique file content is stored once in `CIE_HOME/store/blobs`. A repeat snapshot of unchanged files writes no new data, and the size-plus-mtime check means it doesn't re-read them either.
- Every blob is re-hashed on restore, so corrupt content is never written back.
- Snapshots record files that didn't exist yet, so a restore also removes files a change created.
- Restores of whole-project snapshots keep files added since, unless `remove_added` is set.
- Snapshots taken by the old system still restore, and now get a safety snapshot first.
- `workflow.snapshot_create` and `workflow.snapshot_restore` are kept for compatibility and use the new store.
- A failed automatic snapshot blocks the write and explains why. It can be turned off with `storage.auto_snapshot: false`.

### Knowledge details
- The indexer now records asset metadata:
  - **Images:** size, mode, frames, alpha, and whether it's power-of-two.
  - **WAV audio:** sample rate, channels, bit depth, and duration.
  - **3D models:** OBJ vertex and face counts; glTF/GLB meshes, vertices, triangles, materials, animations, and skins; STL triangle counts.
  - Unreadable assets are flagged, and don't stop indexing.
- The index version is now 3, so each project re-indexes once automatically.
- A background re-indexer runs inside the CIE server every 60 seconds (`knowledge.interval_sec`, minimum 10). Disable it with `knowledge.background_index: false`.
- Decisions, issues (open, fixed, wontfix), architecture notes, history, preferences, and reusable snippets are stored as versioned vault notes under `Projects/<project>/`.
- `knowledge.summary` returns one compact overview, under 4 KB in tests, in place of many separate calls.

### Orchestration details
- `orchestrate.plan` matches plain-language tasks using game-dev synonyms. A task like "sprites" plus "sound" covers both areas.
- Plans start with project context, run read-only checks in parallel, and allow at most 2 change steps, each marked as needing approval.
- Plans flag steps whose tool or local model is missing and suggest a built-in alternative.
- `orchestrate.run` runs read-only steps in parallel and changes one at a time, through the normal permission gate with `approved=False`, so it can never approve anything itself.
- It retries temporary errors twice, reuses identical read results, skips steps that depend on a failure, and supports `$steps.<id>.<field>` references.
- The `cie_discover` tool description now points Claude to `orchestrate.plan`, `knowledge.summary`, and `storage.snapshot`.

### Routing details
- For each task it picks one of three options:
  - a deterministic CIE method (free, local, exact), when the job is measurable;
  - an optional local model, only if installed;
  - a **Claude model and effort level**, plus whether to run it on a separate helper.
- Five modes:

| Mode | Simple | Normal | Hard |
|---|---|---|---|
| balanced (default) | Haiku, low | Sonnet, medium | Opus, high |
| max_quality | Sonnet, medium | Opus, high | Fable, high (falls back to Opus, xhigh) |
| lowest_tokens | Haiku, low | Haiku, medium | Sonnet, high |
| fastest | Haiku, low | Haiku, medium | Sonnet, medium |
| privacy | Haiku, low | Sonnet, medium | Opus, high, with a note that Claude runs on Anthropic's servers |

- Difficulty comes from the wording of the task (hard-problem words versus simple ones), how many files it touches, and how long the request is.
- Large or hard tasks are marked to run on a helper. Small tasks stay in the conversation, because a helper starts with no context and re-reads what it needs. Lowest-tokens mode keeps normal tasks in the conversation for the same reason.
- **How the choice takes effect:** CIE can't switch the model of the conversation calling it. Claude applies the choice by starting a helper (the Agent tool) with that model and effort, which the CIE skill instructs. For the main conversation, CIE suggests switching models only when a task needs a stronger model than the current one.
- Per-difficulty overrides go in `routing.model_overrides` in CIE's config. No paid API key is needed.

### Template details
There are 11 built-in templates, covering every kind in the plan:

| Kind | Templates |
|---|---|
| Project | Hearth browser game (follows the owner's Hearth rules), browser game layout |
| Code | Luau strict module, validated Roblox RemoteEvent, Godot 4 top-down player |
| UI | Accessible pause menu |
| 2D | Generated tileset guide PNG |
| 3D | UV cube (OBJ + MTL) |
| Material | glTF PBR material |
| Animation | Easing presets |
| Workflow | Asset audit |

- Validation actually checks the files: JSON parses, Python compiles, OBJ face indices are in range, PNGs decode, brackets balance, Luau starts with `--!strict`, no placeholders are left, and the Hearth rules hold.
- `templates.apply` validates first, never overwrites, and snapshots the new files so `storage.restore` removes them.

## Fixes found while building
- **Test-ordering race:** parallel first use of storage could fail with "duplicate column name". Storage creation is now locked, and the migration tolerates a column another process already added. A regression test covers it.
- **`models/` folder was excluded from indexing:** the exclude list was meant for AI weights, but it hid every game's 3D models folder. AI weights are now skipped by file type instead (`.gguf`, `.safetensors`, `.pt`, `.pth`, `.onnx`, `.ckpt`, `.pb`).
- **Closed issues kept their "open" tag:** the vault always merged tags. `save_note` now has `replace_tags`, which defaults to off so existing behavior is unchanged.
- **Permissions made consistent:** notes and templates you'd see (`knowledge.record`, `knowledge.issue_status`, `templates.save`) are writes and need approval, like `memory.save`. Only CIE's own internal data skips approval: snapshots, index, and archives.

## Verification (Linux sandbox, Python 3.13)
- `python -m pytest -q`: 136 passed, 6 skipped. 2 failed, both `trimesh` tests, because `trimesh` can't be installed in the sandbox; they passed on the owner's Windows machine before Phase 1.
- Phase 1 tests: 41 passed, with 0 failures in 25 repeated runs after the race fix.
- Every built-in template validates, and negative tests confirm the validator catches broken files.
- CIE's own security check on the repo found 0 high-severity findings.
- Not yet run: the owner's Windows test run, and hosted CI on this commit.

## Known limits
- `.blend`, `.fbx`, and Roblox place files get basic metadata only: format and size.
- Each automatic snapshot of a whole project (before exec capabilities) adds one database row per file. Retention keeps this bounded at 30 days, but very large projects tested often will grow the index. Watch `storage.report`.
- The planner uses keyword matching, so unusual wording can miss a capability. `cie_discover` is the fallback.
- Code checks for Luau, GDScript, and JS are structural (brackets, `--!strict`), not full parsers. Full static analysis is Phase 2.
