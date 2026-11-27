from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from cie import config
from scripts.build_wheelhouse import (
    wheel_metadata, wheel_contents_inventory, prepare_output_directory,
    read_exact_requirements, verify_build_system,
)
from scripts.build_standalone import write_runtime_sbom
from scripts.check_dependency_policy import (
    collect_dependency_tree, canonical_name, contains_forbidden_license,
    direct_dependency_registry_issues, conditional_transitive_dependency_issues,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_load_config_sanitizes_malformed_known_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CIE_HOME", str(home))
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "autonomy_level": "not-a-mode",
        "projects": "not-a-list",
        "approved_tools": ["safe", 7],
        "allowlisted_commands": ["bad"],
        "context_budget_tokens": True,
        "dashboard": {"host": "", "port": 90000},
        "version": "2.3.2.dev0",
    }), encoding="utf-8")
    loaded = config.load_config()
    assert loaded["version"] == "2.3.2"
    assert loaded["autonomy_level"] == "assist"
    assert loaded["projects"] == []
    assert loaded["approved_tools"] == []
    assert isinstance(loaded["allowlisted_commands"], dict)
    assert loaded["context_budget_tokens"] == config.default_config()["context_budget_tokens"]
    assert loaded["dashboard"] == {"host": "127.0.0.1", "port": 8765}


def test_resolve_rejects_missing_or_non_directory_roots(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    cfg = {"projects": [{"id": "missing", "path": str(missing)}]}
    with pytest.raises(PermissionError, match="No existing approved"):
        config.resolve_in_projects("new.txt", cfg)


def test_resolve_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    link = root / "external-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    cfg = {"projects": [{"id": "approved", "path": str(root)}]}
    with pytest.raises(PermissionError):
        config.resolve_in_projects(str(link / "secret.txt"), cfg)
    with pytest.raises(PermissionError):
        config.resolve_in_projects("external-link/secret.txt", cfg)


def test_project_roots_discards_invalid_and_network_roots(tmp_path: Path) -> None:
    valid = tmp_path / "valid"
    valid.mkdir()
    roots = config.project_roots({"projects": [
        {"id": "ok", "path": str(valid)},
        {"id": "blank", "path": " "},
        {"id": "bad-excludes", "path": str(valid), "excludes": "nope"},
        {"id": "network", "path": "//server/share"},
        3,
    ]})
    assert [item["id"] for item in roots] == ["ok"]


def test_iter_project_files_honors_binary_flag(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "code.py").write_text("print('ok')", encoding="utf-8")
    (root / "data.exe").write_bytes(b"MZ")
    root_entry = {"id": "project", "path": str(root)}
    ordinary = {p.name for p in config.iter_project_files(root_entry)}
    all_files = {p.name for p in config.iter_project_files(root_entry, include_binary=True)}
    assert ordinary == {"code.py"}
    assert all_files == {"code.py", "data.exe"}


def test_package_cli_entrypoint_works(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["CIE_HOME"] = str(tmp_path / "home")
    env["CIE_VAULT"] = str(tmp_path / "vault")
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    version = subprocess.run(
        [sys.executable, "-m", "cie", "--version"], cwd=PROJECT_ROOT,
        env=env, check=True, capture_output=True, text=True,
    )
    assert version.stdout.strip() == "cie 2.3.2.dev0"
    doctor = subprocess.run(
        [sys.executable, "-m", "cie", "doctor"], cwd=PROJECT_ROOT,
        env=env, check=True, capture_output=True, text=True,
    )
    assert '"healthy": true' in doctor.stdout.lower()


def test_mcp_binary_smoke_flags_work_without_starting_server(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["CIE_HOME"] = str(tmp_path / "home")
    env["CIE_VAULT"] = str(tmp_path / "vault")
    version = subprocess.run(
        [sys.executable, "mcp_server.py", "--version"], cwd=PROJECT_ROOT,
        env=env, check=True, capture_output=True, text=True,
    )
    assert version.stdout.strip() == "2.3.2.dev0"


def test_wheel_metadata_reader_extracts_name_and_version(tmp_path: Path) -> None:
    path = tmp_path / "demo-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo-1.2.3.dist-info/METADATA", "Metadata-Version: 2.1\nName: Demo\nVersion: 1.2.3\n\n")
    assert wheel_metadata(path) == ("Demo", "1.2.3")


def test_dependency_tree_closure_is_recursive_and_normalized() -> None:
    tree = [{
        "package": {"package_name": "creative_intelligence_engine", "installed_version": "2.3.2.dev0"},
        "dependencies": [{
            "package": {"package_name": "PyYAML", "installed_version": "6.0.2"},
            "dependencies": [{
                "package": {"package_name": "packaging", "installed_version": "24.2"},
                "dependencies": [],
            }],
        }],
    }]
    deps = collect_dependency_tree(tree, "creative-intelligence-engine")
    assert deps == {
        "creative-intelligence-engine": "2.3.2.dev0",
        "pyyaml": "6.0.2",
        "packaging": "24.2",
    }
    assert canonical_name("Tree_Sitter") == "tree-sitter"


def test_license_gate_does_not_misclassify_lgpl_as_gpl() -> None:
    assert not contains_forbidden_license("LGPL-2.1-only")
    assert not contains_forbidden_license("GNU Lesser General Public License v2 or later (LGPLv2+)")
    assert contains_forbidden_license("GPL-3.0-only")
    assert contains_forbidden_license("GNU General Public License v3 (GPLv3)")
    assert contains_forbidden_license("AGPL-3.0-or-later")
    assert contains_forbidden_license("GNU Affero General Public License v3")


def test_offline_verifier_rejects_wheel_hash_mismatch(tmp_path: Path) -> None:
    import hashlib

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    manifest = {
        "schema_version": 1,
        "project": {"name": "creative-intelligence-engine", "version": "2.3.2.dev0"},
        "direct_pins": {"numpy": "2.2.3"},
        "wheels": [{"filename": wheel.name, "sha256": hashlib.sha256(b"different").hexdigest()}],
    }
    (wheelhouse / "wheelhouse-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "verify_offline_install.py"), str(wheelhouse)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "SHA-256 mismatch" in result.stderr


def test_offline_verifier_rejects_unmanifested_archives(tmp_path: Path) -> None:
    import hashlib

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    (wheelhouse / "unexpected.tar.gz").write_bytes(b"sdist")
    manifest = {
        "schema_version": 1,
        "project": {"name": "creative-intelligence-engine", "version": "2.3.2.dev0"},
        "direct_pins": {"numpy": "2.2.3"},
        "wheels": [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}],
    }
    (wheelhouse / "wheelhouse-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "verify_offline_install.py"), str(wheelhouse)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "unexpected_entries" in result.stderr


def test_wheelhouse_builder_refuses_to_delete_untracked_files(tmp_path: Path) -> None:
    output = tmp_path / "wheelhouse"
    output.mkdir()
    unrelated = output / "important.zip"
    unrelated.write_bytes(b"user data")
    with pytest.raises(RuntimeError, match="without a valid prior manifest"):
        prepare_output_directory(output, tmp_path / "project")
    assert unrelated.read_bytes() == b"user data"


def test_wheelhouse_builder_recycles_only_manifested_verified_wheels(tmp_path: Path) -> None:
    import hashlib

    output = tmp_path / "wheelhouse"
    output.mkdir()
    wheel = output / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"previous wheel")
    (output / "wheelhouse-manifest.json").write_text(json.dumps({
        "wheels": [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]
    }), encoding="utf-8")
    prepare_output_directory(output, tmp_path / "project")
    assert list(output.iterdir()) == []


def test_wheelhouse_builder_refuses_modified_previous_wheel(tmp_path: Path) -> None:
    import hashlib

    output = tmp_path / "wheelhouse"
    output.mkdir()
    wheel = output / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"modified wheel")
    (output / "wheelhouse-manifest.json").write_text(json.dumps({
        "wheels": [{"filename": wheel.name, "sha256": hashlib.sha256(b"original").hexdigest()}]
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not matching previous manifest"):
        prepare_output_directory(output, tmp_path / "project")
    assert wheel.read_bytes() == b"modified wheel"


def test_wheelhouse_builder_refuses_project_root_as_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = project / "keep.whl"
    marker.write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="must not be the project root"):
        prepare_output_directory(project, project)
    assert marker.exists()


def test_wheel_inventory_records_license_files_and_native_binaries(tmp_path: Path) -> None:
    path = tmp_path / "soundfile-0.13.1-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("soundfile-0.13.1.dist-info/METADATA", "Metadata-Version: 2.1\nName: soundfile\nVersion: 0.13.1\nLicense: BSD-3-Clause\n\n")
        archive.writestr("_soundfile_data/COPYING", b"native-library-license")
        archive.writestr("_soundfile_data/LICENSE.txt", (
            "Name: libquadmath\n"
            "Files: soundfile/.libs/libquadmath*.so\n"
            "Description: GCC runtime component\n"
            "Availability: https://gcc.gnu.org/\n"
            "License: LGPL-2.1-or-later\n"
        ))
        archive.writestr("_soundfile_data/libsndfile.so", b"native-binary")
    inventory = wheel_contents_inventory(path)
    assert inventory["license_metadata"]["license"] == "BSD-3-Clause"
    assert inventory["license_files"][0]["path"] == "_soundfile_data/COPYING"
    assert inventory["native_binaries"][0]["path"] == "_soundfile_data/libsndfile.so"
    assert inventory["embedded_components"] == [{
        "name": "libquadmath",
        "declared_files": "soundfile/.libs/libquadmath*.so",
        "description": "GCC runtime component",
        "source_url": "https://gcc.gnu.org/",
        "license": "LGPL-2.1-or-later",
        "license_file": "_soundfile_data/LICENSE.txt",
    }]


def test_release_gate_requires_native_lgpl_component_approval() -> None:
    registry = {
        "policy": {"conditional_tier1_licenses": ["LGPL-2.1-only"]},
        "packages": [
            {"name": "soundfile", "version": "0.13.1", "license": "BSD-3-Clause; LGPL-2.1-only native dependency", "tier": 1, "release_approved": True},
            {"name": "libsndfile", "version": None, "license": "LGPL-2.1-only", "tier": "1-native-component", "release_approved": False},
        ],
    }
    issues = direct_dependency_registry_issues(registry, {"soundfile": "0.13.1"}, True)
    assert any("libsndfile" in issue and "not been explicitly approved" in issue for issue in issues)



def test_conditional_transitive_license_requires_exact_registry_approval() -> None:
    licenses = {"certifi": "MPL-2.0"}
    versions = {"certifi": "2026.11.20"}
    direct = {"mcp": "1.30.0"}
    registry = {"policy": {"conditional_tier1_licenses": ["MPL-2.0"]}, "packages": []}
    issues = conditional_transitive_dependency_issues(registry, licenses, versions, direct, True)
    assert any("certifi" in issue and "missing from deps/registry.yaml" in issue for issue in issues)

    registry["packages"] = [{
        "name": "certifi", "version": "2026.11.20", "license": "MPL-2.0",
        "tier": "transitive", "release_approved": False,
    }]
    issues = conditional_transitive_dependency_issues(registry, licenses, versions, direct, True)
    assert any("certifi" in issue and "not been explicitly approved" in issue for issue in issues)

    registry["packages"][0]["release_approved"] = True
    assert conditional_transitive_dependency_issues(registry, licenses, versions, direct, True) == []

    versions["certifi"] = "2027.1.0"
    issues = conditional_transitive_dependency_issues(registry, licenses, versions, direct, True)
    assert any("registry version 2026.11.20 does not match audited version 2027.1.0" in issue for issue in issues)


def test_registry_direct_pin_and_approval_gate_is_fail_closed() -> None:
    registry = {"packages": [{"name": "numpy", "version": "2.2.3", "license": "BSD-3-Clause", "tier": 1, "release_approved": False}]}
    issues = direct_dependency_registry_issues(registry, {"numpy": "2.2.3", "scipy": "1.15.2"}, True)
    assert any("numpy" in issue and "not been explicitly approved" in issue for issue in issues)
    assert any("scipy" in issue and "missing from deps/registry.yaml" in issue for issue in issues)


def test_online_first_ci_covers_supported_python_and_native_platforms() -> None:
    ci = yaml.safe_load((PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    release = yaml.safe_load((PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    supported = ["3.11", "3.12", "3.13"]
    platforms = ["ubuntu-22.04", "windows-2022", "macos-15"]
    assert ci["jobs"]["tests"]["strategy"]["matrix"]["python-version"] == supported
    assert ci["jobs"]["tests"]["strategy"]["matrix"]["os"] == platforms
    assert ci["jobs"]["standalone"]["strategy"]["matrix"]["os"] == platforms
    assert "dependency-audit" in release["jobs"]
    assert "dependency-audit" in release["jobs"]["standalone-bundles"]["needs"]
    assert "offline-wheelhouse" not in ci["jobs"]
    assert "platform-wheelhouses" in release["jobs"]
    assert release["jobs"]["verify-platform-wheelhouses"]["needs"] == "platform-wheelhouses"
    assert release["jobs"]["dependency-audit"]["needs"] == "verify-platform-wheelhouses"
    assert release["jobs"]["standalone-bundles"]["needs"] == "dependency-audit"


def test_build_system_requirements_match_pep517_metadata() -> None:
    import tomllib

    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["build-system"]["requires"]
    expected = {item.split("==", 1)[0].lower(): item.split("==", 1)[1] for item in declared}
    actual = read_exact_requirements(PROJECT_ROOT / "build-system-requirements.txt")
    assert actual == expected


def test_build_system_verification_uses_selected_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from scripts import build_wheelhouse

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(stdout='{"setuptools": "84.2.1", "wheel": "0.48.0"}')

    monkeypatch.setattr(build_wheelhouse.subprocess, "run", fake_run)
    result = verify_build_system(
        {"setuptools": "84.2.1", "wheel": "0.48.0"}, "target-python"
    )
    assert result == {"setuptools": "84.2.1", "wheel": "0.48.0"}
    assert captured["command"][0] == "target-python"


def test_build_system_verification_fails_on_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from scripts import build_wheelhouse

    monkeypatch.setattr(
        build_wheelhouse.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout='{"setuptools": "82.0.1"}'),
    )
    with pytest.raises(RuntimeError, match="does not match build-system-requirements"):
        verify_build_system({"setuptools": "84.2.1"}, "target-python")


def test_standalone_sbom_includes_runtime_and_embedded_native_components(tmp_path: Path) -> None:
    notice = tmp_path / "THIRD_PARTY_LICENSES" / "numpy" / "LICENSE.txt"
    notice.parent.mkdir(parents=True)
    notice.write_text(
        "Name: OpenBLAS\n"
        "Files: numpy.libs/libscipy_openblas*.dll\n"
        "Description: bundled as a dynamically linked library\n"
        "Availability: https://github.com/OpenMathLib/OpenBLAS/\n"
        "License: BSD-3-Clause\n",
        encoding="utf-8",
    )
    manifest = {
        "project": "creative-intelligence-engine",
        "distributions": [
            {
                "name": "numpy",
                "version": "2.2.3",
                "license": "BSD-3-Clause",
                "license_files": [{"path": "THIRD_PARTY_LICENSES/numpy/LICENSE.txt"}],
            },
            {
                "name": "certifi",
                "version": "2026.11.20",
                "license": "MPL-2.0",
                "license_files": [],
            },
        ],
    }
    sbom = write_runtime_sbom(tmp_path, manifest)
    components = {item["name"]: item for item in sbom["components"]}
    assert set(components) == {"numpy", "certifi", "OpenBLAS"}
    assert components["certifi"]["version"] == "2026.11.20"
    assert components["OpenBLAS"]["licenses"][0]["license"]["name"] == "BSD-3-Clause"
    assert sbom["metadata"]["component"]["properties"][0]["value"].startswith("generated-from-actual")
    dependency_map = {item["ref"]: item["dependsOn"] for item in sbom["dependencies"]}
    assert "pkg:pypi/numpy@2.2.3" in dependency_map
    assert dependency_map["pkg:pypi/numpy@2.2.3"] == [
        "urn:cie:embedded:openblas:BSD-3-Clause"
    ]
    saved = json.loads((tmp_path / "sbom.json").read_text(encoding="utf-8"))
    assert saved == sbom
