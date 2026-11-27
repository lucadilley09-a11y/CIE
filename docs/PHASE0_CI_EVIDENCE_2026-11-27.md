# Phase 0 CI evidence — 2026-11-27

## Current result

**Phase 0 is not complete.** The latest hosted CI run provides useful cross-platform test and packaging evidence, but the dependency release gate and owner-machine Windows acceptance test are still open.

Repository: `lucadilley09-a11y/CIE`  
Commit tested: `cf4f86f915b1c3c3aaed4c10012315512f6f9249`

## Hosted CI

Run: https://github.com/lucadilley09-a11y/CIE/actions/runs/45951757222

- Test matrix: 9/9 jobs succeeded — Windows Server 2022, Ubuntu 22.04, and macOS 15; Python 3.11, 3.12, and 3.13.
- Standalone packaging: 3/3 jobs succeeded — Windows, Ubuntu, and macOS.
- Each standalone job executed the packaged binary's `--version` and `--doctor` commands before packaging. This is a hosted-runner smoke test, not the owner's local Windows test.
- All three .mcpb archives were uploaded. The following SHA-256 values are the GitHub Actions artifact ZIP digests, not hashes of the inner .mcpb files:

| Platform artifact | Artifact ID | GitHub artifact ZIP SHA-256 |
|---|---:|---|
| Windows Server 2022 | 15177027541 | `dc81257aa5e8079bf4bb9aa1a7920240d18fb7f29b1eb8f0ca40bc5579abd60d` |
| macOS 15 | 15177336819 | `3a78f22fe970ddc5ae34a43a65dd3be25e953c8a3055b63cc93774bd7593cb4f` |
| Ubuntu 22.04 | 15177656027 | `ff5b7f14c3c809007f4024d122cab949e5fcdeafcd60db2d1d0962faa1ffa913` |

## Dependency and security audit

Run: https://github.com/lucadilley09-a11y/CIE/actions/runs/45951757607  
Audit artifact: https://github.com/lucadilley09-a11y/CIE/actions/runs/45951757607/artifacts/15177590476  
Artifact ZIP SHA-256: `a90c3fcb650719c6dda6fa53dd9fc6b028a8960cf8c084099de210dcaa535129`

- `pip-audit --strict` completed with **no known vulnerabilities** in the audited installed environment.
- The runtime dependency tree contained 35 distributions, including CIE.
- The observed runtime license set contained no GPL/AGPL packages.
- `certifi==2026.11.20` is MPL-2.0 and remains a conditional-license review item.
- The standalone builder copies runtime dependency license/notice files and records file hashes in `THIRD_PARTY_LICENSES.json`; PyInstaller build-tool license/exception files are also collected.
- The audit's release-policy step correctly failed because all eight direct Tier 1 dependencies remain explicitly unapproved.

## Why approval remains blocked

The CI evidence does **not** yet satisfy the registry's full release-approval requirements. In particular, the current report does not provide a verified upstream provenance/attestation record, a per-platform direct-wheel hash manifest for all direct and transitive dependencies, or an independent legal review of conditional/native redistribution obligations. Passing tests and successful executable builds are not substitutes for those records.

The eight direct dependencies remain fail-closed in `deps/registry.yaml`. No release tag should be created while the release audit fails.

The macOS PyInstaller log also reports SDK-version warnings for bundled SciPy binaries. The executable smoke test passed, but code-signing/hardened-runtime readiness is not established by that result.

## Remaining acceptance work

1. Complete evidence-backed package provenance, wheel-hash, transitive-license, and conditional-license review; only then consider changing individual registry approvals.
2. Resolve or document the macOS SciPy SDK warning and confirm required license notices in the actual downloaded bundles.
3. Run the final Windows standalone bundle on the owner's Windows PC, verify it starts and reports a healthy doctor result without system Python.
4. Rerun CI and the release audit after any changes. Phase 0 can be closed only when every mandatory gate has evidence.
