# Changelog

## 2.3.2.dev0: Phase 1 core and storage (2026-11-27)

See `PHASE1_PROGRESS.md` for the full report.

- **Storage (1.3):** deduplicated, losslessly compressed snapshot store; automatic snapshot before every write/exec capability; preview-first restore with a safety snapshot; 30-day retention keeping named snapshots; 5 GB warning; usage, duplicate, compare, lossless PNG, and archive capabilities. Legacy snapshots still restore.
- **Knowledge (1.4):** asset metadata (images, WAV, OBJ/glTF/GLB/STL) in the unified index (index version 3); background re-indexer in the CIE server; decisions, issues, and architecture notes as vault notes; `knowledge.summary`.
- **Orchestration (1.1):** `orchestrate.plan` and `orchestrate.run` (parallel reads, gated changes, retries, reuse, step references).
- **Routing (1.2):** `routing.recommend` picks a free built-in method, an installed local model, or a Claude model (Haiku/Sonnet/Opus/Fable) and effort level (low to max) per task, and whether to run it on a helper. Five modes, plus config overrides.
- **Templates (1.5):** 11 validated built-in templates, including a Hearth-ready browser game; user templates with provenance.
- **Fixes:** storage initialization race; `models/` folders no longer excluded; issue status tags now replace; user-visible note/template writes are approval-gated.

## 2.3.2.dev0 — Phase 0.2/0.3 update (2026-11-27)

### Project registration (Phase 0.2)
- New `cie/projects.py` with five capabilities: `project.list`, `project.detect` (read) and `project.register`, `project.update`, `project.remove` (write, approval-gated under `assist`). No JSON editing or Python commands needed.
- Auto-detects Godot, Unity, Unreal, Roblox (Rojo/Rokit/Wally/place files), Minecraft Fabric/Forge/NeoForge mods and packs, Python, Node, Rust, Go, Gradle, .NET, Blender, and plain web projects, and adds that type's build/cache folders to excludes.
- Validates paths: absolute, existing, non-network, not a drive root, no reused id, no overlap with another registered project.
- `project.remove` edits config and drops CIE's own regenerable index rows only; project files are never touched.
- New CLI: `cie project add|list|detect|remove`.

### Unified index (Phase 0.3)
- `eff.incremental_index` is now the single writer for files, FTS text, symbols, and dependencies. Previously symbols were written only by `coding.brain` and dependencies were never stored, so `cie_status`, `coding.brain`, and `coding.dependencies` disagreed.
- `coding.brain` and `coding.dependencies` refresh the index incrementally, then read from it, so all three report the same counts. `cie_status` index stats now include per-project counts and last-indexed time.
- `coding.dependencies` output is bounded: summary (`by_confidence`, `top_targets`) plus at most `max_edges` raw edges (default 100), with `truncated`/`omitted` flags. It previously returned up to 300 edges (~78 KB) with a silently wrong total.
- Added Lua/Luau symbol and `require` extraction; JavaScript/TypeScript imports now record the module path instead of the imported name; C/C++ `#include` extraction.
- Safe migration: an `index_meta` table records the index version per project; a version change triggers exactly one full re-index.
- Deleted files now also lose their dependency rows; per-file indexing errors are reported instead of silently skipped.

### CI security scanning
- New `security` job in `ci.yml`: Bandit (fails on high-severity findings), Gitleaks over full git history with redacted output, and CIE's own Tier-1 `security_checks` run against CIE itself. Reports upload as the `cie-security-reports` artifact.
- `.gitleaks.toml` allowlists only the intentional fake secrets in the redaction tests.

### Windows-only targets
- Owner decision: CIE targets Windows only. CI test and standalone matrices, release wheelhouses, and release bundles run on `windows-2022` only.
- `scripts/verify_wheelhouse_evidence.py` gains `--platforms` (default: all three, so existing behavior is unchanged when omitted); the release workflow passes `--platforms Windows`.

### Fixes
- Regression test that the dependency-policy parser accepts the flat pipdeptree `--json-tree` shape (parser fix itself landed separately as "Support pipdeptree JSON tree format").
- Added `tests/test_phase0_registration_and_index.py` (10 regression tests).

## 2.3.2.dev0 — Phase 0 candidate (2026-11-26)

### Packaging and verification groundwork
- Added PEP 517 project metadata, CLI entry point, Windows build wrapper, and platform-native PyInstaller/MCPB build script.
- Added cross-platform CI/release workflow definitions and scripts for target-specific wheelhouse creation, SHA-256 manifest verification, fresh-venv no-index installation, and transitive dependency/license policy checks.
- Added a dependency registry with exact direct pins and explicit pending/blocked approval state for unverified packages and native components.
- Hardened config validation and atomic writes; preserved environment overrides; added project-root/path/symlink checks and binary-file filtering.
- Added regression tests for configuration, packaging, dependency policy, offline-manifest integrity, CLI startup, job cleanup, and gitleaks temporary-report cleanup.
- Ensured the MCPB manifest preserves platform-native CIE data-directory defaults; standalone packaging now copies and hashes runtime dependency license/notice files, honors active dependency markers/extras, verifies installed requirements, and fails closed if one is missing. Expanded wheelhouse CI matrices to Python 3.11, 3.12, and 3.13 on each configured runner OS.

### Current validation and limitations
- Local Python 3.13/Linux test results: pytest 77 passed / 6 skipped; unittest 53 run / 6 skipped / 0 failures; compileall passed.
- A CIE-only project wheel and fresh-venv CLI/doctor smoke test passed, but this did not include runtime dependencies.
- Full wheelhouse resolution, complete offline installation, pip-audit/license/transitive-dependency audit, PyInstaller bundles, remote CI, and exact network-disabled Windows/Linux/macOS VM gates remain unverified.
- This is a development candidate, not a complete or release-approved Phase 0.

---

## 1.2.1 — 2026-11-26

### Security and correctness
- Changed the fresh-install autonomy default to `assist` and migrated pre-1.2.1 configurations to the safe default.
- Kept `observe` strictly read-only even when a capability has a saved approval.
- Routed memory actions through the shared capability registry and permission checks.
- Required explicit owner approval for destructive memory restore/import/export operations, including while autonomous mode is enabled.
- Added capability input validation for required/optional fields, supported types, enum values, numeric ranges, unknown arguments, and a 2 MB serialized-input limit.
- Added recursive redaction of secret-like values in structured tool outputs.
- Hardened memory version restore, import, and export against traversal, symlink, and overlapping-directory attacks.

### Reliability and efficiency
- Added job timeout state and cooperative cancellation; documented that arbitrary Python threads cannot be safely force-killed.
- Changed smart context to use token-budget estimates and report which estimator was used, while retaining legacy byte-budget compatibility.
- Added regression tests for safe defaults, permission enforcement, schema validation, secret redaction, timeouts, and vault path handling.

### Validation
- `python -m compileall -q .`: passed.
- `PYTHONPATH=. python -m pytest -q`: 46 passed, 6 skipped.
- `PYTHONPATH=. python -m unittest discover -s tests -q`: 50 tests, all passing except 6 expected optional-tool skips.
- Skips are for optional tools not installed in the review environment: Docker, gdtoolkit, fast-simplification, FluidSynth/soundfont, py-spy, and Ruff.