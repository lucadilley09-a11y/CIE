# Optional disconnected-install testing (not a Phase 0 gate)

The project owner has approved online-first installation. The previous mandatory network-disabled VM gates are therefore retired. First-time installation and updates may download the pinned dependency set from the configured package index; runtime remains local-first.

The wheelhouse builder and verifier are retained for anyone who later needs cached or disconnected installation:

```bash
python -m pip install -r build-system-requirements.txt
python scripts/build_wheelhouse.py --output wheelhouse
python scripts/verify_offline_install.py wheelhouse
```

Build a separate wheelhouse for each operating system, CPU architecture, and Python minor version. Do not reuse a wheelhouse across platforms. The generated manifest records wheel names, versions, sizes, and SHA-256 hashes. These scripts are optional and do not block Phase 0 completion.

For required Windows, Ubuntu, and macOS checks, see [`INSTALL_AND_PLATFORM_TESTING.md`](INSTALL_AND_PLATFORM_TESTING.md). Hosted CI runners build native artifacts; the owner only needs to perform a local Windows smoke test.
