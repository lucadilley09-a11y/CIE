from __future__ import annotations

import json
from pathlib import Path

import pytest

from cie import config


def test_cie_home_and_vault_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "custom-home"
    vault = tmp_path / "custom-vault"
    monkeypatch.setenv("CIE_HOME", str(home))
    monkeypatch.setenv("CIE_VAULT", str(vault))
    assert config.get_cie_home() == home
    assert config.get_vault_root() == vault
    assert home.is_dir() and vault.is_dir()


def test_save_config_is_atomic_and_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "cie-home"
    monkeypatch.setenv("CIE_HOME", str(home))
    saved = config.save_config({**config.default_config(), "autonomy_level": "assist"})
    path = home / "config.json"
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert not list(home.glob(".config-*.tmp"))


def test_save_config_rejects_malformed_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "cie-home"))
    with pytest.raises(ValueError):
        config.save_config({"autonomy_level": "unrestricted"})
    with pytest.raises(TypeError):
        config.save_config({"projects": "not-a-list"})
    with pytest.raises(TypeError):
        config.save_config({"approved_tools": [1, 2]})


def test_gitleaks_temp_report_is_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Verify cleanup on both successful and failed scanner executions without
    # requiring the optional gitleaks executable.
    from cie import external

    monkeypatch.setenv("CIE_HOME", str(tmp_path / "cie-home"))
    project = tmp_path / "project"
    project.mkdir()
    config.save_config({**config.default_config(), "projects": [{"id": "p", "path": str(project)}]})
    monkeypatch.setattr(external, "find_tool", lambda _name: "/fake/gitleaks")
    def fake_run(argv, **kwargs):
        Path(argv[-1]).write_text("[]", encoding="utf-8")
        return {"returncode": 0, "timed_out": False}
    monkeypatch.setattr(external, "_run", fake_run)
    result = external.security_gitleaks(".")
    assert result["status"] == "ok"
    assert not list((tmp_path / "cie-home").glob("cie-gitleaks-*.json"))


def test_atomic_write_failure_preserves_previous_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "cie-home"
    monkeypatch.setenv("CIE_HOME", str(home))
    original = {**config.default_config(), "autonomy_level": "assist"}
    config.save_config(original)
    config_path = home / "config.json"
    before = config_path.read_bytes()
    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(OSError):
        config.save_config({**original, "autonomy_level": "observe"})
    assert config_path.read_bytes() == before
    assert not list(home.glob(".config-*.tmp"))


def test_platform_specific_cie_home_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIE_HOME", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert config.get_cie_home() == tmp_path / "local" / "CIE"

    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.get_cie_home() == tmp_path / "Library" / "Application Support" / "CIE"

    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.get_cie_home() == tmp_path / "xdg" / "CIE"


def test_existing_legacy_cie_home_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".cie"
    legacy.mkdir()
    (legacy / "config.json").write_text("{}", encoding="utf-8")
    assert config.get_cie_home() == legacy


def test_mcp_manifest_does_not_override_platform_data_home() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((project_root / "manifest.json").read_text(encoding="utf-8"))
    assert "CIE_HOME" not in manifest["server"]["mcp_config"].get("env", {})


def test_standalone_builder_copies_runtime_dependency_license_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from scripts import build_standalone

    project = SimpleNamespace(
        metadata={"Name": "creative-intelligence-engine"},
        version="2.3.2.dev0",
        requires=["DemoDep==1.0"],
        files=[],
        locate_file=lambda item: tmp_path / str(item),
    )
    dep_file = Path("demo_dep-1.0.dist-info/licenses/LICENSE")
    dep_source = tmp_path / str(dep_file)
    dep_source.parent.mkdir(parents=True)
    dep_source.write_text("demo license text", encoding="utf-8")
    dependency = SimpleNamespace(
        metadata={"Name": "DemoDep"},
        version="1.0",
        requires=[],
        files=[dep_file],
        locate_file=lambda item: tmp_path / str(item),
    )
    monkeypatch.setattr(
        build_standalone.importlib_metadata, "distributions", lambda: [project, dependency]
    )

    stage = tmp_path / "bundle"
    stage.mkdir()
    result = build_standalone.collect_runtime_license_files(
        stage, "creative-intelligence-engine"
    )
    copied = stage / "THIRD_PARTY_LICENSES" / "DemoDep-1.0" / dep_file
    assert copied.read_text(encoding="utf-8") == "demo license text"
    assert result["runtime_distribution_count"] == 1
    assert result["distributions"][0]["name"] == "DemoDep"


def test_standalone_builder_refuses_runtime_dependency_without_license_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from scripts import build_standalone

    project = SimpleNamespace(
        metadata={"Name": "creative-intelligence-engine"}, version="2.3.2.dev0",
        requires=["DemoDep==1.0"], files=[], locate_file=lambda item: tmp_path / str(item),
    )
    dependency = SimpleNamespace(
        metadata={"Name": "DemoDep"}, version="1.0", requires=[], files=[],
        locate_file=lambda item: tmp_path / str(item),
    )
    monkeypatch.setattr(
        build_standalone.importlib_metadata, "distributions", lambda: [project, dependency]
    )
    stage = tmp_path / "bundle"
    stage.mkdir()
    with pytest.raises(RuntimeError, match="missing distributable license"):
        build_standalone.collect_runtime_license_files(stage, "creative-intelligence-engine")


def test_standalone_license_walk_ignores_inactive_extras_and_honors_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from scripts import build_standalone

    project = SimpleNamespace(
        metadata={"Name": "creative-intelligence-engine"}, version="2.3.2.dev0",
        requires=["DemoDep==1.0"], files=[], locate_file=lambda item: tmp_path / str(item),
    )
    def make_dependency(name: str, reqs: list[str] | None = None):
        license_file = Path(f"{name.lower()}-1.0.dist-info/licenses/LICENSE")
        source = tmp_path / str(license_file)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"{name} license", encoding="utf-8")
        return SimpleNamespace(
            metadata={"Name": name}, version="1.0", requires=reqs or [],
            files=[license_file], locate_file=lambda item: tmp_path / str(item),
        )
    dependency = make_dependency(
        "DemoDep", ["InactiveExtra; extra == 'test'", "ActiveChild; python_version >= '3.11'"]
    )
    inactive = make_dependency("InactiveExtra")
    active = make_dependency("ActiveChild")
    monkeypatch.setattr(
        build_standalone.importlib_metadata, "distributions",
        lambda: [project, dependency, inactive, active],
    )
    stage = tmp_path / "bundle"
    stage.mkdir()
    result = build_standalone.collect_runtime_license_files(
        stage, "creative-intelligence-engine"
    )
    names = {item["name"] for item in result["distributions"]}
    assert names == {"DemoDep", "ActiveChild"}


def test_standalone_license_walk_fails_when_required_dependency_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from scripts import build_standalone

    project = SimpleNamespace(
        metadata={"Name": "creative-intelligence-engine"}, version="2.3.2.dev0",
        requires=["MissingDep==1.0"], files=[], locate_file=lambda item: tmp_path / str(item),
    )
    monkeypatch.setattr(
        build_standalone.importlib_metadata, "distributions", lambda: [project]
    )
    stage = tmp_path / "bundle"
    stage.mkdir()
    with pytest.raises(RuntimeError, match="Required runtime dependency MissingDep"):
        build_standalone.collect_runtime_license_files(stage, "creative-intelligence-engine")


def test_standalone_license_walk_fails_on_wrong_direct_dependency_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from scripts import build_standalone

    project = SimpleNamespace(
        metadata={"Name": "creative-intelligence-engine"}, version="2.3.2.dev0",
        requires=["DemoDep==1.0"], files=[], locate_file=lambda item: tmp_path / str(item),
    )
    dependency = SimpleNamespace(
        metadata={"Name": "DemoDep"}, version="2.1", requires=[], files=[],
        locate_file=lambda item: tmp_path / str(item),
    )
    monkeypatch.setattr(
        build_standalone.importlib_metadata, "distributions", lambda: [project, dependency]
    )
    stage = tmp_path / "bundle"
    stage.mkdir()
    with pytest.raises(RuntimeError, match="does not satisfy ==1.0"):
        build_standalone.collect_runtime_license_files(stage, "creative-intelligence-engine")


def test_standalone_license_walk_includes_requested_dependency_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from scripts import build_standalone

    def distribution(name: str, requirements: list[str] | None = None):
        license_file = Path(f"{name.lower()}-1.0.dist-info/licenses/LICENSE")
        source = tmp_path / str(license_file)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"{name} license", encoding="utf-8")
        return SimpleNamespace(
            metadata={"Name": name}, version="1.0", requires=requirements or [],
            files=[license_file], locate_file=lambda item: tmp_path / str(item),
        )

    project = distribution("creative-intelligence-engine", ["DemoDep[fast]==1.0"])
    dependency = distribution("DemoDep", ["FastChild; extra == 'fast'"])
    fast_child = distribution("FastChild")
    monkeypatch.setattr(
        build_standalone.importlib_metadata, "distributions",
        lambda: [project, dependency, fast_child],
    )
    stage = tmp_path / "bundle"
    stage.mkdir()
    result = build_standalone.collect_runtime_license_files(
        stage, "creative-intelligence-engine"
    )
    assert {item["name"] for item in result["distributions"]} == {"DemoDep", "FastChild"}
