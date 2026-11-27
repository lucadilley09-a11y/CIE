# Phase 0 CI evidence — 2026-11-27

## Current result

**Phase 0 is not complete.** The latest hosted CI run provides useful cross-platform test and packaging evidence, but the dependency release gate and owner-machine Windows acceptance test are still open.

Repository: `lucadilley09-a11y/CIE`  
Commit tested: `6388b632afa4fb27a05343f58ad701d5417916c5`

## Hosted CI

Latest run: https://github.com/lucadilley09-a11y/CIE/actions/runs/45954378823 (supersedes earlier successful run 45951757222)

- Test matrix: 9/9 jobs succeeded — Windows Server 2022, Ubuntu 22.04, and macOS 15; Python 3.11, 3.12, and 3.13.
- Standalone packaging: 3/3 jobs succeeded — Windows, Ubuntu, and macOS.
- Each standalone job executed the packaged binary's `--version` and `--doctor` commands before packaging. This is a hosted-runner smoke test, not the owner's local Windows test.
- All three .mcpb archives were uploaded. The following SHA-256 values are the GitHub Actions artifact ZIP digests, not hashes of the inner .mcpb files:

| Platform artifact | Artifact ID | GitHub artifact ZIP SHA-256 |
|---|---:|---|
| Windows Server 2022 | 15178048470 | `ed5b4a4030d2c26725ec3e9a947ea158b6d8016c483584065d0836f6bb2c4d9a` |
| macOS 15 | 15178113283 | `9385cc82cdc17a2b3eed1dc6ee5681f56a5c66c27bdfebaaae6cc25b03f10358` |
| Ubuntu 22.04 | 15177883167 | `b4e4a819bdb31970ce860f72674ed847a2a1d99a6a7f8dcc9e3cfd4dba049b16` |

## Dependency and security audit

Latest run: https://github.com/lucadilley09-a11y/CIE/actions/runs/45954378756  
Audit artifact: https://github.com/lucadilley09-a11y/CIE/actions/runs/45954378756/artifacts/15177957526  
Artifact ZIP SHA-256: `d86f3bf09bcb6df1223ec9774eb5a4448882535566728f3a258dea327a4f3130`

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
