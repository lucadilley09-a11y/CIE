# CIE v2.3.2 — Current independent review packet

**Review target:** online-first Phase 0 candidate plus consolidated master plan  
**Review posture:** review source correctness and plan consistency; do not infer unperformed acceptance gates.

## Product-plan changes in this snapshot

- Added `MASTER_DEVELOPMENT_PLAN.md` as the consolidated source for the product objective, constraints, architecture, capability scope, phase order, and definition of done.
- Added persistent code and asset storage using filesystem artifacts, SQLite metadata, SHA-256 content addressing, FTS5/symbol retrieval, provenance, compatibility checks, and version history.
- Added project snapshots/rollback, unified testing and verification, environment/dependency doctor, project documentation/knowledge index, and verified reusable component library.
- Added the adaptive AI model and reasoning-effort router. It considers task difficulty, task-specific quality, token use, actual cost, latency, context window, tool/vision support, reasoning controls, privacy, availability, rate limits, and measured historical performance. It supports task decomposition, deterministic no-AI execution, budget-aware escalation, user modes, provider exclusions, and opt-in local/external models.
- Explicitly documents the MCP boundary: CIE cannot silently switch Claude's host-selected model or internal reasoning effort. It can route only its own authorized model calls, prepare compact context, and recommend host changes.
- Places router foundations in Phase 1 and adaptive task-level routing/performance learning in Phase 2.
- Owner-approved policy update: online-first installation and updates; local-first runtime. Paid APIs and hosted CIE services remain nonessential. Wheelhouses and disconnected VM tests are optional, not Phase 0 gates.
- Replaced mandatory offline-wheelhouse CI with native standalone builds on hosted Windows, Ubuntu, and macOS runners. Release workflow now audits dependencies/licenses before producing standalone artifacts.

## Current-session local checks

Environment: Linux x86_64, CPython 3.13.5.

- `python -m pytest -q`: **84 passed, 6 skipped**.
- `python -m unittest discover -s tests -q`: **53 tests run, 6 skipped, 0 failures**.
- `python -m compileall -q cie mcp_server.py dashboard.py scripts tests`: passed.
- CI/release workflow YAML, dependency registry YAML, project TOML, MCPB manifest JSON, and SBOM JSON parsed successfully.
- `SOURCE_MANIFEST.sha256`: all 55 listed files verified.
- The six skipped pytest integrations require optional tools not installed in this environment: Docker, gdtoolkit, fast-simplification, FluidSynth/soundfont, py-spy, and Ruff.

## Phase 0 acceptance remains open

1. Run and review `pip-audit`, `pip-licenses`, `pipdeptree`, and the CIE dependency-policy report in a clean environment. Resolve vulnerabilities, unknown/prohibited licenses, and native-component obligations. All nine direct runtime dependencies currently remain `release_approved: false` until that review is complete.
2. Build and smoke-test PyInstaller bundles on hosted Windows, Ubuntu, and macOS runners; verify startup without system Python and preservation of third-party notices.
3. Run the supported Python test matrix on hosted Windows, Ubuntu, and macOS runners.
4. Run a local Windows smoke test on the owner's PC.
5. Retain actual remote CI links, logs, and artifact hashes.

The execution environment has no package-index connectivity, so a fresh venv could not install the project/audit/build tools. No dependency audit or standalone executable is claimed. GitHub Actions has not been triggered from this environment. See `docs/PHASE0_AUDIT_AND_ACTION_REPORT.md` and `docs/INSTALL_AND_PLATFORM_TESTING.md`.

## Reviewer questions

- Are the online-first/local-first product constraints and free-core requirements internally consistent?
- Are CI and release workflows correct, secure, and sufficiently fail-closed?
- Does the standalone packaging script correctly collect the full runtime dependency closure and all license notices on Windows, Linux, and macOS?
- Are dependency registry entries accurate and are conditional/native licenses handled correctly?
- Are persistent artifact storage, rollback, verification, project knowledge, reusable components, and the model/reasoning router scoped clearly enough to implement without unbounded autonomy, token use, or resource consumption?
- Is the Claude MCP model/effort control boundary stated accurately and consistently?
- Does the source snapshot introduce any regression beyond the workflow/documentation changes? Re-run tests rather than relying on reported results.

## Reviewer discipline

Report findings by severity with file/section references. Distinguish confirmed defects from recommendations and unverified assumptions. Do not claim dependency audit, cross-platform, or standalone success without direct evidence. Offline installation is optional under the current owner-approved policy.
