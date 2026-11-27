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

The latest Windows artifact was downloaded and inspected directly: the outer Actions ZIP contained the `.mcpb`; the `.mcpb` SHA-256 is `b67d6bce11e8c91d9e18d2e8c1d98fc43f617861960088e14d7adcc90c88ccfa`. Its archive contains the Windows executable, manifest, build info, SBOM, CIE license, a `THIRD_PARTY_LICENSES.json` manifest, and 63 third-party license-file entries covering 37 runtime distributions. The PyInstaller build-tool `COPYING.txt` is also present and explicitly includes the bootloader exception. This verifies notice-file presence in this Windows artifact, not a complete legal review or the user's local launch test.

## Remaining acceptance work

1. Complete evidence-backed package provenance, wheel-hash, transitive-license, and conditional-license review; only then consider changing individual registry approvals.
2. Resolve or document the macOS SciPy SDK warning and confirm required license notices in the actual downloaded bundles.
3. Run the final Windows standalone bundle on the owner's Windows PC, verify it starts and reports a healthy doctor result without system Python.
4. Rerun CI and the release audit after any changes. Phase 0 can be closed only when every mandatory gate has evidence.


## Expanded platform wheel and native-license evidence

Follow-up workflow: https://github.com/lucadilley09-a11y/CIE/actions/runs/45956196754

- Built 37 Linux wheels, 37 macOS wheels, and 38 Windows wheels, including the CIE project wheel, for 112 platform wheel artifacts total.
- The verifier checked each external wheel SHA-256 against the corresponding PyPI release JSON metadata. All three platform wheelhouses passed.
- The evidence records wheel SHA-256 values, license/notice file hashes, native binary hashes, package license metadata, PyPI source/project URLs, and a generated CycloneDX SBOM. Evidence artifact: https://github.com/lucadilley09-a11y/CIE/actions/runs/45956196754/artifacts/15178633308
- Platform coverage observed: Linux x86_64, Windows AMD64, and macOS arm64.
- pip-audit again reported no known vulnerabilities.
- Wheel-level inspection found additional bundled native components in NumPy/SciPy that were not visible in the ordinary Python dependency tree:
  - GCC runtime library — GPL-3.0-with-GCC-exception
  - libquadmath — LGPL-2.1-or-later
  - OpenBLAS and LAPACK — BSD-3-Clause-Attribution (the NumPy OpenBLAS notice also reports BSD-3-Clause)
- These components and certifi==2026.11.20 (MPL-2.0) are registered as conditional/native release-review items. Their approvals remain false; the evidence collection does not silently approve them.
- The release audit therefore remains intentionally blocked by eight unapproved direct dependencies, four native components, and the MPL-2.0 certifi transitive dependency. No release tag should be created.
- The expanded wheel-evidence gate passed on all three operating systems. The full CI test matrix is being rerun after updating its gate assertion to include the new wheelhouse workflow; the owner's local Windows acceptance test remains outstanding.


## Final automated Phase 0 verification (2026-11-27)

This section supersedes earlier "pending approval" and "pending build" notes above.

### Results

- Latest CI run: https://github.com/lucadilley09-a11y/CIE/actions/runs/45958230092 — **success**.
  - 9/9 OS/Python test matrix jobs passed (Windows, Ubuntu, macOS; Python 3.11, 3.12, 3.13).
  - 3/3 standalone MCPB builds passed; each executed \`--version\` and \`--doctor\` smoke tests.
- Latest release audit: https://github.com/lucadilley09-a11y/CIE/actions/runs/45958230100 — **success**.
  - \`pip-audit\`: no known vulnerabilities.
  - Dependency/license policy passed for 37 runtime distributions.
- Platform wheel evidence: 112 wheels across macOS arm64 (37), Linux x86_64 (37), and Windows AMD64 (38). Every external wheel SHA-256 matched the corresponding PyPI release JSON digest.
- Evidence artifact: https://github.com/lucadilley09-a11y/CIE/actions/runs/45958230100/artifacts/15179871639
  - Includes per-platform wheel hashes, license/notice file hashes, native binary hashes, PyPI source metadata, and a generated CycloneDX 1.5 SBOM (117 components).
- \`certifi==2026.11.20\` is now an exact direct runtime pin, eliminating the earlier mismatch between the audit environment and standalone bundle.

### Windows candidate

- Artifact: https://github.com/lucadilley09-a11y/CIE/actions/runs/45958230092/artifacts/15179359044
- MCPB SHA-256: \`a1b55c0240012512b79b6202654dc51dc7783f9574ae54037992a657379df651\`
- Standalone package contains a generated CycloneDX SBOM with 37 runtime distributions plus 4 embedded native components.
- \`THIRD_PARTY_LICENSES.json\` records 37 runtime distributions and 62 runtime license/notice files; the PyInstaller bootloader license/exception is also included.
- \`BUILD_INFO.json\` confirms a self-contained Python runtime and successful Windows-hosted smoke tests.

### License and provenance notes

- Direct dependencies have exact pins, upstream release-tag references, and platform wheel hashes recorded.
- NumPy/SciPy native notices identify OpenBLAS, LAPACK, GCC runtime library, and libquadmath. Their license notices are preserved in the bundle and their conditional/native approvals are recorded as project engineering decisions.
- The wheel notices identify intended upstream sources, but the exact provenance of those bundled native binary builds is not independently attested. This evidence is not a legal opinion.

### Remaining acceptance

Automated Phase 0 gates are green. The only remaining acceptance action is the owner's local Windows import/launch check of the attached MCPB candidate. No final release tag has been created pending that local check.
