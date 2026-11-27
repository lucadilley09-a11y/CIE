# CIE v2.3.2 — Phase 0 implementation and acceptance status

**Implementation status: Phase 0 source candidate assembled and locally regression-tested.**

**Acceptance status: NOT COMPLETE.** The owner has changed the installation requirement to online-first, so disconnected-installation and wheelhouse gates are no longer mandatory. The remaining mandatory gates are dependency/security/license audit evidence, native standalone builds, and actual hosted CI results. Do not treat this snapshot as a release.

## Current owner-approved installation policy

CIE is online-first for installation and updates, and local-first at runtime. No paid API, subscription, hosted CIE service, or external creative application is required by the free core. Wheelhouses and fully disconnected installation are optional. The later section titled “Owner-approved change: online-first installation” supersedes the historical offline-first gates recorded below.

## Product design constraint carried forward

CIE is primarily a Claude-facing MCP tool, not a standalone dashboard-first product. Across all phases, prioritize automatic execution of routine safe workflows, low-token context transfer, compact structured tool results, caching/reuse of retrieved context, and minimal repeated user instructions. Keep approval prompts for permission boundaries, destructive changes, external access, and other meaningful risk. The CLI/dashboard remain secondary direct-use surfaces. This is an implementation priority, not permission to bypass the frozen safety and approval model.

## Scope implemented in this candidate

- Reconstructed the original 33-file CIE v1.2.1 text source bundle into a conventional project tree; versioned the package as `2.3.2.dev0` and the MCPB manifest as `2.3.2`.
- Added PEP 517/setuptools packaging, `cie` CLI entry point, runtime and build-only requirements separation, a dedicated exact-pinned `build-system-requirements.txt`, Windows build wrapper, PyInstaller spec, and standalone MCPB builder. The MCPB manifest no longer overrides platform-specific CIE data directories.
- Added CI and release workflows, a dependency registry, dependency/license policy checker, optional wheelhouse builder, manifest/hash verification, and a fresh-venv install verifier. The current CI workflow uses hosted Windows, Ubuntu, and macOS runners; its native standalone-build matrix replaces mandatory wheelhouse matrices.
- Hardened config loading and validation; preserved `CIE_HOME` and `CIE_VAULT`; made config writes atomic; rejected invalid project roots and path/symlink escapes; added binary-file filtering for project iteration.
- Added job-future/cancellation-handle cleanup and regression coverage.
- Changed the gitleaks report path to a private temporary file, suppresses matched secret text from results, and removes the report in `finally`.
- Kept Tier-1 dependency approvals fail-closed. The registry marks unverified packages and native LGPL obligations as not release-approved. The standalone builder now collects license/notice files across the installed runtime dependency closure, hashes them into `THIRD_PARTY_LICENSES.json`, and refuses to bundle if an installed runtime distribution has no license text.

## Local validation executed

Environment: Python 3.13.5, Linux x86_64 container. These results apply only to this local environment.

| Check | Result |
|---|---|
| `python -m compileall -q cie mcp_server.py dashboard.py scripts tests` | Passed |
| `python -m pytest -q` | **77 passed, 6 skipped** |
| `python -m unittest discover -s tests -q` | **53 tests run, 6 skipped, 0 failures** |
| CI/release YAML syntax parse | Passed in local YAML parser |
| CIE-only wheel build using local build backend (`--no-build-isolation --no-deps`) | Passed; intentionally excludes runtime dependencies |
| Fresh-venv install of that CIE-only wheel with `--no-index --no-deps`, followed by version and doctor smoke checks | Passed; `cie --version` and doctor returned healthy. Optional runtime modules were absent by design, so this validates the application wheel/CLI only, not the complete dependency wheelhouse |

Project-only wheel artifact: `creative_intelligence_engine-2.3.2.dev0-py3-none-any.whl`, 73,101 bytes, SHA-256 `d3f386aacc07c8ef508d2096afd476e9ca8cc28687060c3ccda02d6981c6af54`. The build used the container's locally installed setuptools 82.0.1 with `--no-build-isolation --no-deps`; it did **not** validate the pinned isolated build backend (`setuptools==84.2.1`, `wheel==0.48.0`).

Six skipped tests cover optional tools unavailable in this container: Docker, gdtoolkit, fast-simplification, FluidSynth/soundfont, py-spy, and Ruff. A skip is not counted as a pass for the optional integration.

## Blocking acceptance gates — not passed

1. **Complete wheelhouse:** `scripts/build_wheelhouse.py` could not resolve the pinned build/runtime artifacts because this execution environment has no working package-index network access. Only the dependency-free CIE project wheel was built locally. The wheelhouse build failed while resolving the isolated build backend: DNS resolution for the package index failed while fetching `setuptools==84.2.1`. The complete CIE wheel plus all pinned direct/transitive dependency wheels has not been assembled here.
2. **Offline installation:** the full `scripts/verify_offline_install.py` flow has not been completed against a full wheelhouse. The project-only smoke test above does not satisfy this gate.
3. **Dependency/security audit:** `pip-audit`, `pip-licenses`, and `pipdeptree` are not available in this environment; no complete CVE or transitive-license audit has been executed. Direct runtime packages remain `release_approved: false`. `soundfile`/`libsndfile` requires an explicit LGPL redistribution review.
4. **Standalone bundles:** PyInstaller is not installed here; no platform-native self-contained executable or `.mcpb` has been built and tested.
5. **External clean VMs:** no genuinely network-disabled Windows 10 x64, Windows 11 x64, Ubuntu 22.04 x64, or macOS 14 arm64 VM acceptance run has been performed.
6. **Cross-platform CI:** the workflow is configured for Ubuntu 22.04, Windows Server 2022, and macOS 15 arm64 runners, but no remote GitHub Actions run was triggered or observed from this environment. Hosted runner checks would still not replace the exact Windows client/macOS 14 VM gates.

## Dependency registry interpretation

- `pyproject.toml` is the source of truth for exact direct runtime pins.
- `deps/registry.yaml` records package purpose, declared license, target tier, and whether provenance, transitive dependencies, wheels, native components, and release approval remain unverified.
- A per-platform `wheelhouse-manifest.json` is generated only after the wheelhouse build succeeds and records each actual wheel filename, version, size, and SHA-256.
- The dependency-policy report is generated from the installed package's `pipdeptree` closure plus `pip-licenses`; no such complete report exists for this candidate because the audit tools and resolved environment were unavailable. The standalone builder copies license/notice files for the runtime closure and records their hashes, but that new packaging path has only unit-test coverage. It has not yet been exercised against the complete locked dependency environment or inside a real PyInstaller bundle. The local dependency environment is incomplete: `mcp`, `tree-sitter`, PyInstaller, pip-audit, pip-licenses, and pipdeptree are absent.
- No pending package is approved merely because a license string is listed. The actual distribution artifacts and their transitive/native components must be audited.

## Source audits and known implementation details

- `cie/vault.py` is present and ends with a complete `register_capabilities()` implementation; the full module compiles. No source truncation was found in the extracted baseline, so no speculative rewrite was made.
- `cie/creative.py` uses one `_blender_run()` invocation in each reviewed Blender export/render path; no duplicate call was found in those paths.
- The gitleaks report cleanup is protected by `finally`; errors and successful results do not return the matched secret value.
- Job futures and cancellation flags are removed by completion callbacks.
- The standalone builder traverses the installed CIE runtime dependency closure using active environment markers and requested extras, verifies required dependencies and version constraints are installed, copies license/notice files (including the bundled `libsndfile` COPYING text), and hashes them into `THIRD_PARTY_LICENSES.json`. Unit tests verify marker handling, missing-dependency rejection, version mismatch rejection, path safety, and fail-closed behavior when license text is absent. This path has not yet been exercised against the complete locked dependency environment.
- The MCPB manifest template omits `CIE_HOME` overrides so platform-native data directory defaults are not defeated.
- `creative.py` and `workflows.py` retain the baseline's existing capabilities; they are not placeholder stubs for Phase 0.

## Current required evidence before Phase 0 can be accepted

- Run and archive `pip-audit`, `pip-licenses`, `pipdeptree`, and the CIE license-policy report in a clean environment; resolve vulnerabilities, unknown/prohibited licenses, and document native-library redistribution obligations. Explicitly approve only audited package versions in the registry.
- Build and smoke-test PyInstaller bundles on hosted Windows, Ubuntu, and macOS runners; confirm each executable starts without invoking system Python and includes third-party license notices.
- Run the supported Python test matrix on hosted Windows, Ubuntu, and macOS runners.
- Run a local Windows smoke test on the owner's Windows machine.
- Record actual CI job links/results and preserve artifact hashes.

Wheelhouse generation, `--no-index` install verification, and fully network-disabled VM tests are optional under the current owner-approved online-first policy. Until every current mandatory gate has evidence, Phase 0 remains **not complete** and Phase 1 should not be treated as release-ready groundwork.

## Additional independent verification (2026-11-26)

A second local run in a clean working extraction reconfirmed ZIP integrity (50 members), compileall, YAML parsing for both workflows and the dependency registry, and pytest (**77 passed, 6 skipped**). The dependency-policy command was attempted and failed closed because `pip-licenses` is absent. The wheelhouse builder was attempted and failed because package-index DNS resolution prevented fetching the pinned `setuptools==84.2.1` build requirement. See `PHASE0_VALIDATION_RUN_2026-11-26.md` for exact evidence and remaining blockers. Phase 0 remains NOT COMPLETE.

## Additional implementation hardening (2026-11-26)

A second review identified a destructive edge case in `scripts/build_wheelhouse.py`: the prior implementation could delete arbitrary archive files from a caller-supplied output directory before building. The builder now refuses project-root/parent outputs and symlink destinations, and only recycles a non-empty wheelhouse when its prior manifest accounts for every entry and every existing wheel still matches its recorded SHA-256. The offline verifier additionally rejects symlinked wheelhouse/wheel files and validates recorded wheel sizes when present.

Regression coverage was added for untracked-file preservation, safe manifest-based recycling, modified-wheel rejection, and project-root output rejection.

Latest local results in this environment:

- `python -m pytest -q`: **81 passed, 6 skipped**.
- `python -m unittest discover -s tests -q`: **53 tests run, 6 skipped, 0 failures**.
- `python -m compileall -q cie mcp_server.py dashboard.py scripts tests`: passed.
- CI/release/dependency-registry YAML parsing: passed.
- CIE application wheel built with `--no-deps --no-build-isolation`; installed into a fresh venv with `--no-index --no-deps`; `cie --version` and `cie doctor` succeeded. This deliberately does not prove runtime dependency closure or full offline install.
- `cie doctor` reports healthy with the stdlib-only app wheel; optional runtime modules are absent in that smoke-test venv.

The complete wheelhouse attempt was repeated and failed at isolated build dependency resolution because package-index DNS is unavailable (`setuptools==84.2.1` could not be fetched). The dependency policy audit still cannot run because `pip-licenses` is not installed; `pip-audit`, `pipdeptree`, and PyInstaller are also unavailable. These gates remain open. No network-disabled target VM or remote GitHub Actions execution was performed.

## Latest local verification (2026-11-26)

A fresh run against the packaged candidate produced **81 passed, 6 skipped** with pytest, and **53 tests run, 6 skipped, 0 failures** with unittest discovery. Compilation, source-manifest hashes, YAML parsing, direct-pin/registry consistency, JSON parsing, and a CIE-only wheel build plus fresh-venv `--no-index --no-deps` CLI/doctor smoke test passed. The complete wheelhouse build was retried and remains blocked by package-index DNS failure. The detailed evidence is in `PHASE0_VALIDATION_RUN_2026-11-26.md`.

The CIE-only wheel smoke test is deliberately limited: it does not include or validate the runtime dependency closure. The full dependency wheelhouse, full offline install, security/license audits, PyInstaller bundles, remote CI, and exact network-disabled Windows/Linux/macOS VM gates remain open. **Phase 0 is still NOT COMPLETE.**


## Build-backend determinism improvement (2026-11-27)

The independent reviewer recommended explicitly capturing PEP 517 build requirements so an offline build does not unexpectedly attempt to fetch `setuptools` or `wheel`. Added `build-system-requirements.txt` as the single exact-pin source for the backend requirements declared in `pyproject.toml`. `scripts/build_wheelhouse.py` now verifies the selected build interpreter has those exact versions before touching the output directory, records both the required pins and verified versions in `wheelhouse-manifest.json`, and invokes `pip wheel --no-build-isolation`. CI/release wheelhouse jobs install the pinned backend before building. The manual offline runbook now requires staging a separate build-tools wheelhouse for offline source builds; build-time tools remain separate from Tier-1 runtime dependencies.

This change removes implicit build-isolation downloads and fails early if the selected interpreter is not provisioned with the exact backend versions. It does not itself create or verify the missing build-tools wheelhouse or complete runtime wheelhouses. Those package acquisition and platform acceptance gates remain open.

Latest additional local run: `python -m pytest -q` reports **84 passed, 6 skipped**; `python -m unittest discover -s tests -q` reports **53 tests run, 6 skipped, 0 failures**; compileall and YAML/TOML parsing pass. The new tests confirm build-system pins match `pyproject.toml`, the selected interpreter is queried, and version mismatches fail closed. The current container has setuptools 82.0.1 and no installed `wheel` distribution, so the pinned build environment check correctly blocks before modifying the wheelhouse output. Full package acquisition and external gates remain outstanding.

## Current-session revalidation and roadmap update (2026-11-26)

The candidate was re-extracted and tested in the current Linux x86_64 / CPython 3.13.5 environment:

- `python -m pytest -q`: **84 passed, 6 skipped**.
- `python -m compileall -q cie mcp_server.py dashboard.py scripts tests`: passed.
- The six skips remain optional integrations unavailable in this environment; they are not counted as passes.
- The full offline wheelhouse, dependency/security audit, standalone bundle, clean network-disabled VM, and remote CI gates remain open. This run does not change the Phase 0 acceptance status.

Added [`MASTER_DEVELOPMENT_PLAN.md`](MASTER_DEVELOPMENT_PLAN.md) as the consolidated product and implementation plan. It incorporates the persistent project/code/asset store, snapshots and rollback, unified verification, environment doctor, project knowledge index, verified component library, and the adaptive model/reasoning-effort router. The router is sequenced across Phase 1 and Phase 2 and explicitly cannot claim to change Claude's host-selected model or internal effort through MCP. The frozen v2.3.2 Phase 0 acceptance requirements remain unchanged.


## Owner-approved change: online-first installation (2026-11-26)

The owner confirmed that CIE does not need to support offline installation as a requirement. This supersedes the previous offline-first acceptance gates. Normal setup may download pinned dependencies; runtime remains local-first, with no mandatory paid APIs or hosted CIE service.

### Removed from mandatory Phase 0 acceptance
- Complete per-platform/Python wheelhouse matrices.
- Full `--no-index` installation and genuinely network-disabled VM gates.
- Offline build-tool wheelhouse staging.

The wheelhouse builder and verifier remain optional tools. They are not deleted because cached/disconnected installation may be useful later.

### Current mandatory Phase 0 gates
1. Run the full test/CLI matrix on hosted Windows, Ubuntu, and macOS runners across supported Python versions.
2. Run `pip-audit`, `pip-licenses`, and `pipdeptree` in a clean environment; review all vulnerabilities, unknown licenses, GPL/AGPL, LGPL/native components, and registry approval state.
3. Build and smoke-test native standalone bundles on Windows, Ubuntu, and macOS; verify the executable starts without invoking system Python and includes required notices.
4. Run the actual GitHub Actions workflows and retain links, logs, and artifact hashes.
5. Run a local Windows smoke test on the owner's Windows machine. A Chromebook without Linux support is not a blocker; hosted Linux/macOS runners provide those platform builds.

### Changes made in this candidate
- Replaced the mandatory offline-wheelhouse CI job with a native standalone-build matrix.
- Reworked release workflow to perform dependency/security/license audits followed by native standalone builds.
- Added `docs/INSTALL_AND_PLATFORM_TESTING.md` with Windows PowerShell instructions, audit commands, Chromebook guidance, GitHub Actions procedure, and optional wheelhouse instructions.
- Updated README and master plan to reflect online-first installation.

### Current-session evidence
- `python -m compileall -q cie mcp_server.py dashboard.py scripts tests`: passed.
- `python -m pytest -q`: **84 passed, 6 skipped**.
- Network package installation was attempted in a fresh virtual environment but blocked by DNS/package-index unavailability. Consequently, `pip-audit`, `pip-licenses`, `pipdeptree`, and PyInstaller could not be installed or run here. No dependency audit or native standalone artifact is claimed.
- GitHub Actions cannot be triggered from this local execution environment. The updated workflows must be pushed to the repository and run from its Actions tab.

**Phase 0 remains NOT COMPLETE.** The updated plan removes offline work as a blocker, but dependency auditing, standalone builds, and actual CI evidence are still required.
