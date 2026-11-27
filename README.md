# Creative Intelligence Engine (CIE)

CIE is primarily a Claude-facing MCP capability layer for coding, asset, 3D, animation, audio, and memory workflows, with a direct command-line interface as a secondary entry point. Its design target is high automation with low interaction and token overhead: CIE should perform routine orchestration, retrieval, checks, and reporting itself, while asking for approval only when permissions or meaningful risk require it. The v2.3.2 specification is frozen; this checkout is the Phase 0 implementation candidate, not a release.

## Requirements

- Python 3.11, 3.12, or 3.13 for the source/development install.
- The exact runtime direct pins are in `pyproject.toml`.
- Blender, Godot, Unity, Unreal, local AI models, and other external tools are optional integrations; none is required to install or start the core MCP server.

## Development install

From the repository root, in a connected development environment:

```bash
python -m pip install -r requirements.txt
python -m pip install pytest==9.2.0
python -m compileall -q cie mcp_server.py dashboard.py scripts tests
python -m pytest -q
python -m unittest discover -s tests -v
```

`requirements.txt` installs the project and the exact runtime dependencies declared by `pyproject.toml`. Test tooling is deliberately kept out of the Tier-1 runtime dependency set.

## CLI and MCP server

```bash
python -m cie --version
python -m cie doctor
python -m cie config
python -m cie project detect "C:\path\to\game"
python -m cie project add "C:\path\to\game" --id my_game
python -m cie project list
python -m cie project remove my_game
python mcp_server.py --version
python mcp_server.py --doctor
python mcp_server.py --stdio
```

Projects can also be registered from Claude with the `project.register` capability (approval-gated). The installed `cie` command maps to `cie.__main__:main`. The MCP stdio server is provided by `mcp_server.py`.

## Online-first install and platform testing

CIE uses an online-first installation: pip downloads the pinned runtime dependencies during setup. After installation, the core CIE server and tools run locally; no paid API, hosted CIE service, external creative application, or local AI model is required. Network access is needed for first-time dependency installation and package updates.

Windows setup, dependency audits, standalone builds, Chromebook limitations, and GitHub Actions instructions are documented in [`docs/INSTALL_AND_PLATFORM_TESTING.md`](docs/INSTALL_AND_PLATFORM_TESTING.md).

The wheelhouse builder and no-index verifier remain available as optional tools for users who later want cached or disconnected installs. They are not required Phase 0 release gates under the online-first decision.

## Build a standalone MCPB

On a connected build host for the target operating system and architecture:

```bash
python -m pip install -r requirements.txt
python -m pip install -r build-requirements.txt
python scripts/build_standalone.py --output release-packages
```

The PyInstaller build tool and `packaging` are build-only and are not CIE Tier-1 runtime dependencies. The builder copies and hashes license/notice files for the installed runtime dependency closure into `THIRD_PARTY_LICENSES/` and fails closed if a required runtime package or its license text is missing. The script smoke-tests the produced executable before packaging it, unless `--skip-smoke-test` is explicitly supplied. Standalone bundles must be built on each target platform; the resulting bundle has not yet passed the frozen external VM acceptance gates.

On Windows, `build_windows.ps1` runs the same install, test, and packaging steps.

## Dependency policy and release status

`deps/registry.yaml` records direct runtime pins, declared license metadata, planned Tier-1 dependencies, and explicit verification/approval status. `PENDING`, `UNVERIFIED`, or `release_approved: false` means the package is not cleared for release. Per-platform wheel manifests record actual wheel hashes; `pipdeptree`, `pip-licenses`, and `pip-audit` are required in the release audit. Unknown licenses, unapproved entries, and GPL/AGPL dependencies fail the release gate. LGPL obligations require package-specific review and are not silently treated as permissive.

CIE Phase 0 remains open until direct and transitive package audits, CI evidence across Windows/Ubuntu/macOS, and native standalone-bundle build/smoke tests have recorded evidence. Wheelhouses and fully disconnected VM tests are optional under the online-first decision. The current locally verified results and known blockers are tracked in [`PHASE0_STATUS.md`](PHASE0_STATUS.md).

## Safety defaults

CIE defaults to `assist` autonomy. Read operations can run without approval; writes, execution, and destructive operations are subject to the permission policy and explicit owner approval.
