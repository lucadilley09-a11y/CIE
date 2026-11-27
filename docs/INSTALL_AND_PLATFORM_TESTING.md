# CIE installation, audits, and platform testing

## Product decision: online-first installation

The project owner has relaxed the requirement that normal installation and operation must be offline. CIE is now **online-first for installation and updates**: Python/pip may download pinned dependencies from the configured package index. The application remains local-first at runtime; it does not require a paid API, subscription, cloud-hosted CIE service, or external creative application.

A wheelhouse is no longer a Phase 0 release gate. The existing wheelhouse scripts are retained as an optional future convenience for users who want a cached or disconnected install. Do not spend time building nine OS/Python wheelhouse combinations unless the owner later asks for that feature.

## 1. Install on Windows 10/11

Install Python 3.12 (64-bit) from python.org and enable the option to add Python to PATH. Open PowerShell in the CIE repository folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip install pytest==9.2.0
python -m compileall -q cie mcp_server.py dashboard.py scripts tests
python -m pytest -q
python -m pip check
python -m cie --version
python -m cie doctor
```

If PowerShell blocks activation, do not change machine-wide policy. Run the venv interpreter directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m cie doctor
```

Build the Windows standalone bundle:

```powershell
python -m pip install -r build-requirements.txt
python scripts/build_standalone.py --output release-packages
```

The output is a platform-native `.mcpb` archive under `release-packages`. The build script smoke-tests the executable before packaging it and includes dependency license notices. A successful build is not established until the command actually completes and the artifact is checked.

## 2. Dependency and security audits

Run in a clean Python 3.12 virtual environment, not a general-purpose environment containing unrelated packages:

```powershell
python -m pip install -e .
python -m pip install pip-audit==2.10.1 pip-licenses==5.5.5 pipdeptree==4.2.16
New-Item -ItemType Directory -Force dependency-audit | Out-Null
python -m pip_audit --strict --skip-editable --format=json | Out-File -Encoding utf8 dependency-audit/pip-audit.json
pip-licenses --format=json | Out-File -Encoding utf8 dependency-audit/licenses.json
pipdeptree --json-tree | Out-File -Encoding utf8 dependency-audit/dependency-tree.json
pipdeptree --warn fail
python scripts/check_dependency_policy.py --require-release-approval --output dependency-audit
```

Review vulnerabilities, unknown licenses, GPL/AGPL, conditional LGPL obligations, and native bundled components. A generated report is evidence, not automatic approval. The release policy must pass and any conditional license must have a documented decision.

## 3. Chromebook constraints

A school-managed Chromebook may block Linux development containers and local executable builds. Do not try to bypass school administration controls. Use GitHub Actions for Linux and macOS build/test jobs. If the repository is available on GitHub, push the branch and open the **Actions** tab; the CI workflow runs on hosted Windows, Ubuntu, and macOS runners and uploads standalone artifacts. The Chromebook browser can download/view workflow artifacts if school policy permits.

## 4. Cross-platform coverage

The CI workflow tests Python 3.11/3.12/3.13 on Windows Server 2022, Ubuntu 22.04, and macOS 14 hosted runners. It separately builds and smoke-tests native standalone bundles on all three operating systems. This is practical coverage without requiring the owner to own a Mac or Linux PC. Hosted runners are not identical to every Windows 10/11 device, so retain one local Windows smoke test on the owner's machine.

## 5. Triggering CI and release workflows

1. Commit and push the CIE repository to GitHub.
2. Open the repository's **Actions** tab.
3. Select **CIE CI** and choose **Run workflow** if it has not already triggered on push.
4. Wait for every matrix job to finish. Red jobs are failures, not warnings to ignore.
5. Open each successful standalone job and download its artifact. Check that the `.mcpb` contains the executable, `manifest.json`, `BUILD_INFO.json`, `THIRD_PARTY_LICENSES/`, and `THIRD_PARTY_LICENSES.json`.
6. The dependency/license/security audit now runs automatically on every push and pull request. You only need to inspect its uploaded `cie-dependency-audit` artifact when a run completes. Native release bundles are built only for version tags (`v*`), after the audit passes.

If Actions are disabled by organization/school policy or the repository is not on GitHub, report that blocker; do not claim remote CI ran.

## 6. Optional wheelhouse / disconnected install

The repository still contains `scripts/build_wheelhouse.py` and `scripts/verify_offline_install.py`. These may be used later to cache dependencies or support disconnected installation. They are optional and do not block normal Phase 0 completion under the online-first decision.
