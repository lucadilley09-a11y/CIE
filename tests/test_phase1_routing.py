"""Phase 1.2 routing tests: local-first choices, modes, honest availability."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "home" / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    r = registry.CapabilityRegistry()
    registry.register_all_capabilities(r)
    yield r
    storage._DEFAULT_STORAGE = None


def _rec(reg, task, mode=""):
    res = reg.run("routing.recommend", {"task": task, **({"mode": mode} if mode else {})})
    assert res["status"] == "ok", res
    return res["data"]


def test_measurable_tasks_route_to_builtin_methods(reg) -> None:
    audio = _rec(reg, "is my jump sound too loud or clipping")["recommendations"][0]
    assert audio["category"] == "audio_check" and audio["use"]["route"] == "deterministic"
    code = _rec(reg, "find where the Player class is defined")["recommendations"][0]
    assert code["use"]["capability"] == "coding.symbol_search"


def test_open_ended_work_routes_to_claude(reg) -> None:
    r = _rec(reg, "design a weapon mutation mechanic")["recommendations"][0]
    assert r["use"]["route"] == "claude"


def test_uninstalled_models_are_never_chosen_but_suggested(reg) -> None:
    r = _rec(reg, "transcribe the voice lines", mode="privacy")["recommendations"][0]
    assert r["category"] == "speech"
    assert r["use"]["route"] == "claude"  # whisper not installed, so it can't be the pick
    assert any(o["route"] == "local_model" and not o["available"] for o in r["alternatives"])
    assert "upgrade" in r


def test_modes_change_the_choice(reg) -> None:
    q = _rec(reg, "does my sprite art look consistent", mode="max_quality")["recommendations"][0]
    assert q["use"]["route"] == "claude"
    t = _rec(reg, "does my sprite art look consistent", mode="lowest_tokens")
    assert t["mode"] == "lowest_tokens"
    assert "cannot switch the model of the conversation" in t["limits"]


def test_set_mode_is_gated_and_persists(reg) -> None:
    assert reg.run("routing.set_mode", {"mode": "fastest"})["status"] == "approval_required"
    assert reg.run("routing.set_mode", {"mode": "fastest"}, approved=True)["status"] == "ok"
    assert reg.run("routing.status", {})["data"]["mode"] == "fastest"
    assert reg.run("routing.set_mode", {"mode": "turbo"}, approved=True)["status"] == "error"


def test_model_and_effort_follow_task_difficulty(reg) -> None:
    easy = _rec(reg, "rename this variable")["claude"]
    assert (easy["model"], easy["effort"], easy["delegate_to_helper"]) == ("haiku", "low", False)
    normal = _rec(reg, "fix the jump not working")["claude"]
    assert (normal["model"], normal["effort"]) == ("sonnet", "medium")
    hard = _rec(reg, "redesign the multiplayer replication architecture")["claude"]
    assert (hard["model"], hard["effort"], hard["delegate_to_helper"]) == ("opus", "high", True)
    assert "model 'opus'" in hard["how"] and "effort 'high'" in hard["how"]


def test_modes_shift_model_choice(reg) -> None:
    task = "redesign the multiplayer replication architecture"
    assert _rec(reg, task, mode="max_quality")["claude"]["model"] == "fable"
    assert _rec(reg, task, mode="max_quality")["claude"]["fallback"]["model"] == "opus"
    assert _rec(reg, task, mode="fastest")["claude"]["model"] == "sonnet"
    lt = _rec(reg, "implement the inventory feature", mode="lowest_tokens")["claude"]
    assert lt["delegate_to_helper"] is False  # helpers cost startup tokens
    assert "Anthropic" in _rec(reg, task, mode="privacy")["claude"]["privacy_note"]


def test_large_scope_delegates_and_files_raise_complexity(reg) -> None:
    res = reg.run("routing.recommend", {"task": "write tests for the combat module", "files_involved": 12})["data"]["claude"]
    assert res["delegate_to_helper"] is True and res["complexity"] in {"medium", "high"}


def test_switch_suggestion_only_upgrades(reg) -> None:
    up = reg.run("routing.recommend", {"task": "fix the jump not working", "current_model": "claude-haiku-5-5"})["data"]["claude"]
    assert "sonnet" in up["switch_suggestion"]
    down = reg.run("routing.recommend", {"task": "rename this variable", "current_model": "claude-opus-5-5"})["data"]["claude"]
    assert "switch_suggestion" not in down


def test_model_overrides_from_config(reg) -> None:
    from cie.config import load_config, save_config
    cfg = load_config()
    cfg["routing"] = {"mode": "balanced", "model_overrides": {"medium": {"model": "opus", "effort": "medium"}}}
    save_config(cfg)
    r = _rec(reg, "fix the jump not working")["claude"]
    assert (r["model"], r["effort"]) == ("opus", "medium")
