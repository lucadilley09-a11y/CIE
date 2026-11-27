# Changelog

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