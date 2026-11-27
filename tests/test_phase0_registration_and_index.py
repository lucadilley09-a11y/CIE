"""Phase 0.2/0.3 regression tests: project registration and the unified index."""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def cie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    monkeypatch.setenv("CIE_HOME", str(home))
    monkeypatch.setenv("CIE_VAULT", str(home / "vault"))
    from cie import storage
    storage._DEFAULT_STORAGE = None
    from cie import registry
    registry._REGISTRY = None if hasattr(registry, "_REGISTRY") else None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    yield reg, tmp_path
    storage._DEFAULT_STORAGE = None


def _make_project(root: Path) -> Path:
    proj = root / "game"
    (proj / "src").mkdir(parents=True)
    (proj / "project.godot").write_text("[application]\n", encoding="utf-8")
    (proj / "src" / "main.py").write_text(
        "import os\nfrom pathlib import Path\n\n\ndef run():\n    return 1\n\n\nclass Player:\n    pass\n",
        encoding="utf-8",
    )
    (proj / "src" / "enemy.luau").write_text(
        "local Util = require(script.Parent.Util)\nlocal function spawn()\nend\nfunction Enemy.update(dt)\nend\n",
        encoding="utf-8",
    )
    (proj / ".godot").mkdir()
    (proj / ".godot" / "cache.py").write_text("def skipped(): pass\n", encoding="utf-8")
    return proj


def test_project_register_detects_type_and_excludes(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    res = reg.run("project.register", {"path": str(proj), "project_id": "game"}, approved=True)
    assert res["status"] == "ok", res
    entry = res["data"]["project"]
    assert "godot" in entry["project_types"]
    assert ".godot" in entry["excludes"]
    listed = reg.run("project.list", {})
    assert listed["data"]["count"] == 1
    # Same folder twice is a no-op; reused id or overlapping folder is refused.
    again = reg.run("project.register", {"path": str(proj)}, approved=True)
    assert again["data"]["created"] is False
    other = tmp / "other"
    other.mkdir()
    dup = reg.run("project.register", {"path": str(other), "project_id": "game"}, approved=True)
    assert dup["status"] == "error"
    nested = reg.run("project.register", {"path": str(proj / "src")}, approved=True)
    assert nested["status"] == "error"


def test_project_register_requires_approval_in_assist(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    res = reg.run("project.register", {"path": str(proj)})
    assert res["status"] == "approval_required"


def test_project_register_rejects_bad_paths(cie_env) -> None:
    reg, tmp = cie_env
    assert reg.run("project.register", {"path": str(tmp / "missing")}, approved=True)["status"] == "error"
    assert reg.run("project.register", {"path": "relative/dir"}, approved=True)["status"] == "error"
    assert reg.run("project.register", {"path": "\\\\server\\share"}, approved=True)["status"] == "error"


def test_index_counts_agree_across_status_brain_and_dependencies(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    reg.run("project.register", {"path": str(proj), "project_id": "game"}, approved=True)
    idx = reg.run("eff.incremental_index", {"project_id": "game"})["data"]
    assert idx["symbols_written"] >= 4  # run, Player, spawn, Enemy.update
    assert idx["dependencies_written"] >= 3  # os, pathlib, Util
    from cie.storage import get_storage
    stats = get_storage().index_stats()
    brain = reg.run("coding.brain", {"project_id": "game"})["data"]["projects"][0]
    deps = reg.run("coding.dependencies", {"project_id": "game"})["data"]
    assert stats["symbols_indexed"] == brain["symbols_indexed"] == idx["symbols_written"]
    assert stats["dependencies_indexed"] == brain["dependencies_indexed"] == deps["edge_count"]
    assert stats["files_indexed"] == brain["file_count"]
    # Excluded engine cache folder is not indexed.
    names = {s["name"] for s in reg.run("coding.symbol_search", {"query": "", "project_id": "game", "limit": 50})["data"]["matches"]}
    assert "skipped" not in names
    assert {"run", "Player", "spawn", "Enemy.update"} <= names


def test_index_is_incremental_and_removes_deleted_files(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    reg.run("project.register", {"path": str(proj), "project_id": "game"}, approved=True)
    reg.run("eff.incremental_index", {"project_id": "game"})
    second = reg.run("eff.incremental_index", {"project_id": "game"})["data"]
    assert second["indexed"] == 0 and second["skipped_unchanged"] >= 3
    (proj / "src" / "main.py").unlink()
    third = reg.run("eff.incremental_index", {"project_id": "game"})["data"]
    assert third["removed"] == 1
    from cie.storage import get_storage
    rows = get_storage().query("SELECT COUNT(*) AS c FROM dependencies_index WHERE source_file LIKE ?", ("%main.py",))
    assert rows[0]["c"] == 0


def test_old_index_version_triggers_one_full_reindex(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    reg.run("project.register", {"path": str(proj), "project_id": "game"}, approved=True)
    reg.run("eff.incremental_index", {"project_id": "game"})
    from cie.storage import get_storage
    get_storage().execute("UPDATE index_meta SET index_version = 1 WHERE project_id = ?", ("game",))
    res = reg.run("eff.incremental_index", {"project_id": "game"})["data"]
    assert res.get("full_reindex") == ["game"] and res["indexed"] >= 3
    assert "full_reindex" not in reg.run("eff.incremental_index", {"project_id": "game"})["data"]


def test_dependency_output_is_bounded(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    reg.run("project.register", {"path": str(proj), "project_id": "game"}, approved=True)
    deps = reg.run("coding.dependencies", {"project_id": "game", "max_edges": 1})["data"]
    assert len(deps["edges"]) == 1
    assert deps["truncated"] is True and deps["omitted"] == deps["edge_count"] - 1
    assert deps["top_targets"]


def test_project_remove_keeps_files_and_purges_index(cie_env) -> None:
    reg, tmp = cie_env
    proj = _make_project(tmp)
    reg.run("project.register", {"path": str(proj), "project_id": "game"}, approved=True)
    reg.run("eff.incremental_index", {"project_id": "game"})
    res = reg.run("project.remove", {"project_id": "game"}, approved=True)
    assert res["status"] == "ok" and res["data"]["index_rows_purged"] > 0
    assert (proj / "src" / "main.py").exists()
    from cie.storage import get_storage
    assert get_storage().index_stats()["files_indexed"] == 0


def test_cli_project_commands(cie_env, capsys) -> None:
    _, tmp = cie_env
    proj = _make_project(tmp)
    from cie.__main__ import main
    assert main(["project", "detect", str(proj)]) == 0
    assert main(["project", "add", str(proj), "--id", "game"]) == 0
    assert main(["project", "list"]) == 0
    assert '"game"' in capsys.readouterr().out
    assert main(["project", "remove", "game"]) == 0


def test_dependency_policy_accepts_flat_json_tree() -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from check_dependency_policy import collect_dependency_tree
    flat = [{
        "key": "creative-intelligence-engine",
        "package_name": "creative-intelligence-engine",
        "installed_version": "2.3.2.dev0",
        "dependencies": [{"key": "pyyaml", "package_name": "PyYAML", "installed_version": "6.0.2", "dependencies": []}],
    }]
    assert collect_dependency_tree(flat, "creative-intelligence-engine") == {
        "creative-intelligence-engine": "2.3.2.dev0",
        "pyyaml": "6.0.2",
    }
