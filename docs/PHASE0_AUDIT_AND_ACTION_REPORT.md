# CIE Phase 0 — Audit and action report

**Decision:** online-first installation, local-first runtime. Paid APIs and hosted CIE services remain nonessential and are not part of the free core.

## Current verified evidence in this execution

| Check | Result |
|---|---|
| Python compileall | Passed |
| pytest | 84 passed, 6 skipped |
| unittest discovery | 53 run, 6 skipped, 0 failures |
| CI and release workflow YAML parsing | Passed with PyYAML |
| Dependency registry YAML parsing | Passed |
| Project TOML, MCPB manifest JSON, SBOM JSON parsing | Passed |

The six pytest skips are optional integrations unavailable in this environment: Docker, gdtoolkit, fast-simplification, FluidSynth/soundfont, py-spy, and Ruff.

## Explicitly blocked / not run

- Package-index access failed due DNS resolution failure from this execution environment. A clean virtual environment was created, but installation of the project and audit/build tools could not complete.
- `pip-audit`, `pip-licenses`, and `pipdeptree` were not installed, so no vulnerability or transitive-license report is claimed.
- PyInstaller was not installed, so no native standalone executable or MCPB was produced here.
- GitHub Actions cannot be triggered from this local environment. The workflows must be pushed to the repository and run from the GitHub Actions tab.
- No Windows, Ubuntu, or macOS remote CI job has been observed in this session.

## Registry review finding

All nine exact direct runtime pins are represented in `deps/registry.yaml` at the same versions as `pyproject.toml`. However, all nine currently have `release_approved: false`. This is intentional fail-closed state: they must not be marked approved until the actual installed dependency closure, vulnerability results, license metadata, and native component notices have been reviewed. The `soundfile` / `libsndfile` LGPL obligation requires particular attention.

## Next actions

1. Push this candidate to the CIE GitHub repository.
2. Run **CIE CI**. Require all Windows/Ubuntu/macOS and Python 3.11/3.12/3.13 test jobs to pass; inspect standalone artifacts from each native runner.
3. The **CIE release checks** dependency audit now runs automatically on every push and pull request. Review its vulnerability, license, and dependency-tree artifacts; resolve findings and document each conditional/native license decision before changing registry approval flags. Native release bundles are gated to version tags (`v*`).
4. On the owner's Windows PC, install Python 3.12 and run the commands in `INSTALL_AND_PLATFORM_TESTING.md`; build and smoke-test the Windows standalone bundle locally if desired. The GitHub Windows runner also provides the native build.
5. Only after evidence is retained should Phase 0 be marked complete and Phase 1 begin.

## Wheelhouses

Wheelhouse creation and full disconnected VM testing are now optional. They are retained as a future convenience and are not blockers under the owner's online-first decision.
