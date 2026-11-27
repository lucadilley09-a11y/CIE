# CIE v2.3.2 Phase 0 — Local validation evidence

Date: 2026-11-26 (project date)
Environment: Linux x86_64 container, CPython 3.13.5
Scope: checks executed in this environment only. This is not cross-platform or network-disabled VM evidence.

## Checks passed

| Check | Result | Notes |
|---|---|---|
| Candidate ZIP integrity | Passed | 51 members; `ZipFile.testzip()` returned `None`; no cache directories in archive. |
| Source manifest | Passed | All 50 listed source-file SHA-256 entries matched; no missing entries. |
| `python -m compileall -q cie mcp_server.py dashboard.py scripts tests` | Passed | No syntax compilation errors. |
| `python -m pytest -q` | **84 passed, 6 skipped** | Six skips are optional external integrations unavailable in this container. |
| `python -m unittest discover -s tests -q` | **53 tests run, 6 skipped, 0 failures** | Separate unittest discovery pass. |
| YAML parse: CI workflow, release workflow, dependency registry | Passed | Parsed with installed PyYAML. |
| Dependency registry consistency | Passed | All 9 exact direct runtime pins match registry names, versions, and Tier-1 classification. |
| `manifest.json` and `sbom.json` parse | Passed | Both valid JSON; MCPB manifest reports version 2.3.2. |
| CIE project wheel build | Passed | Built `creative_intelligence_engine-2.3.2.dev0-py3-none-any.whl` using installed setuptools, `--no-deps --no-build-isolation`; SHA-256 `b857261f717591e5883f175936386e37cb654997ff8f966eaf192f1cd9ca760e`. |
| Project-only offline install | Passed | Installed that wheel into a fresh venv with `--no-index --no-deps`; `cie --version` and `cie doctor` succeeded. Optional runtime modules were intentionally absent, so this is not a full application dependency test. |
| Wheelhouse output safety tests | Passed | Regression tests cover preserving untracked files, manifest-based recycling, modified wheel rejection, and refusing project-root output. |
| Build backend pin tests | Passed | Exact build-system pins match `pyproject.toml`; selected interpreter version probing works; mismatched or missing backend versions fail closed. |
| Build backend manifest metadata | Implemented; full manifest pending | Builder records exact build-system requirements and verified versions, and runs `pip wheel --no-build-isolation` after preflight. A complete wheelhouse has not yet been produced. |

## Additional build determinism work

Added `build-system-requirements.txt` with exact `setuptools==84.2.1` and `wheel==0.48.0` pins, kept separate from Tier-1 runtime dependencies. `build-requirements.txt` includes this file for build tooling setup. The wheelhouse builder now verifies the selected Python interpreter has those exact versions before touching the output directory, records both required and observed versions in its manifest, and uses `--no-build-isolation` to prevent implicit package-index access for PEP 517 backend installation. CI/release wheelhouse jobs install the backend pins explicitly. The offline runbook explains that disconnected source builds need a separately staged build-tools wheelhouse.

This environment currently has setuptools 82.0.1 and no `wheel` distribution installed. The new preflight therefore correctly refuses to build; it does not attempt to fetch the pinned backend and does not modify the wheelhouse output. This is a fail-closed behavior check, not a successful build.

## Checks attempted but blocked

| Check | Result | Evidence |
|---|---|---|
| Complete target wheelhouse build | Blocked | `scripts/build_wheelhouse.py` attempted to resolve isolated build requirement `setuptools==84.2.1`; package-index DNS failed (`Temporary failure in name resolution`). |
| `pip-audit`, `pip-licenses`, `pipdeptree` | Blocked | Executables are not installed in this environment; no full CVE or transitive-license audit was produced. |
| PyInstaller standalone build | Blocked | PyInstaller is not installed; package-index network is unavailable. |
| Full no-index fresh-venv install | Not run to completion | No complete wheelhouse exists. The project-only wheel test does not satisfy this gate. |

Six skipped pytest integrations: Docker, gdtoolkit, fast-simplification, FluidSynth/soundfont, py-spy, and Ruff. These are reported as skipped, not passed.

## Mandatory acceptance gates still open

1. Build complete target-specific wheelhouses containing the CIE wheel and every pinned direct/transitive runtime dependency, with manifests and hashes.
2. Run `scripts/verify_offline_install.py` using each complete target wheelhouse in a fresh environment with package-index access disabled.
3. Produce and review `pip-audit`, `pip-licenses`, and `pipdeptree` evidence; resolve unknown/prohibited licenses and explicitly review `soundfile`'s bundled `libsndfile` LGPL obligations. Do not approve dependencies until actual wheel contents and native components are audited.
4. Build and smoke-test standalone PyInstaller bundles on each target platform, confirming startup without system Python.
5. Complete genuinely network-disabled clean VM gates for Windows 10/11 x64, Ubuntu 22.04 x64, and macOS 14 arm64 as defined in `docs/PHASE0_MANUAL_VM_GATES.md`.
6. Run the configured remote CI/release workflows and retain actual job links, logs, and artifact hashes.

## Status

**Phase 0 remains NOT COMPLETE.** Source-level tests, project packaging smoke tests, registry consistency checks, and local artifact integrity checks pass in this Linux environment. Full offline dependency closure, security/license approval, standalone platform builds, and the required external OS/VM tests remain unverified. No claim is made that these gates passed.

## Follow-up verification after build-backend determinism changes

- `python -m pytest -q`: **84 passed, 6 skipped**.
- `python -m unittest discover -s tests -q`: **53 tests run, 6 skipped, 0 failures**.
- `python -m compileall -q cie mcp_server.py dashboard.py scripts tests`: passed.
- CI/release YAML, dependency registry YAML, project TOML, MCPB manifest JSON, and SBOM JSON parsing: passed.
- `SOURCE_MANIFEST.sha256`: regenerated and all 51 listed source files verified.
- Build-wheelhouse preflight: intentionally blocked because the active container has `setuptools==82.0.1` and no installed `wheel`, while the project pins `setuptools==84.2.1` and `wheel==0.48.0`. The builder failed before creating or modifying its output directory, confirming the new fail-closed behavior. This is not a successful wheelhouse build.
