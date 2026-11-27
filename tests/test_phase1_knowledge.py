"""Phase 1.4 knowledge tests: asset metadata, background indexer,
structured project notes, and the compact project summary."""
from __future__ import annotations

import json
import struct
import time
import wave
from pathlib import Path

import pytest


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_path = tmp_path.resolve()  # Windows runners can hand back short/alias paths
    home = tmp_path / "home"
    monkeypatch.setenv("CIE_HOME", str(home))
    monkeypatch.setenv("CIE_VAULT", str(home / "vault"))
    from cie import registry, storage
    storage._DEFAULT_STORAGE = None
    reg = registry.CapabilityRegistry()
    registry.register_all_capabilities(reg)
    proj = tmp_path / "game"
    (proj / "art").mkdir(parents=True)
    (proj / "sfx").mkdir()
    (proj / "models").mkdir()
    from PIL import Image
    Image.new("RGBA", (64, 64)).save(proj / "art" / "tile.png")
    Image.new("RGB", (100, 30)).save(proj / "art" / "banner.png")
    with wave.open(str(proj / "sfx" / "jump.wav"), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
        wf.writeframes(b"\x00\x00" * 11025)
    (proj / "models" / "cube.obj").write_text(
        "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3\nf 1 3 4\n", encoding="utf-8")
    gltf = {"asset": {"version": "2.0"}, "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
            "accessors": [{"count": 24}, {"count": 36}], "nodes": [{}], "materials": [{}], "animations": [{}]}
    payload = json.dumps(gltf).encode()
    payload += b" " * ((4 - len(payload) % 4) % 4)
    glb = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(payload)) + struct.pack("<I4s", len(payload), b"JSON") + payload
    (proj / "models" / "crate.glb").write_bytes(glb)
    (proj / "main.lua").write_text("local function spawn() end\n", encoding="utf-8")
    from cie.projects import register_project
    register_project(str(proj), "game")
    yield reg, proj
    from cie.knowledge import stop_background_indexer
    stop_background_indexer()
    storage._DEFAULT_STORAGE = None


WRITES = {"knowledge.record", "knowledge.issue_status"}


def _ok(reg, cid, args=None):
    res = reg.run(cid, args or {}, approved=cid in WRITES)
    assert res["status"] == "ok", res
    return res["data"]


def test_asset_metadata_is_indexed(env) -> None:
    reg, proj = env
    data = _ok(reg, "knowledge.assets", {"project_id": "game"})
    assert data["counts_by_kind"] == {"image": 2, "audio": 1, "model": 2}
    by_name = {Path(a["path"]).name: a for a in data["assets"]}
    assert by_name["tile.png"]["power_of_two"] is True and by_name["tile.png"]["has_alpha"] is True
    assert by_name["banner.png"]["power_of_two"] is False
    assert by_name["jump.wav"]["duration_sec"] == 0.5 and by_name["jump.wav"]["sample_rate"] == 22050
    assert by_name["cube.obj"]["vertices"] == 4 and by_name["cube.obj"]["faces"] == 2
    assert by_name["crate.glb"]["vertices"] == 24 and by_name["crate.glb"]["triangles"] == 12
    assert by_name["crate.glb"]["animations"] == 1
    npot = _ok(reg, "knowledge.assets", {"project_id": "game", "kind": "image", "where": {"power_of_two": False}})
    assert [Path(a["path"]).name for a in npot["assets"]] == ["banner.png"]


def test_asset_metadata_updates_and_removes(env) -> None:
    reg, proj = env
    _ok(reg, "knowledge.assets", {"project_id": "game"})
    from PIL import Image
    time.sleep(0.01)
    Image.new("RGB", (128, 128)).save(proj / "art" / "banner.png")
    (proj / "sfx" / "jump.wav").unlink()
    data = _ok(reg, "knowledge.assets", {"project_id": "game"})
    by_name = {Path(a["path"]).name: a for a in data["assets"]}
    assert by_name["banner.png"]["width"] == 128 and by_name["banner.png"]["power_of_two"] is True
    assert "jump.wav" not in by_name


def test_unreadable_asset_does_not_break_indexing(env) -> None:
    reg, proj = env
    (proj / "art" / "broken.png").write_bytes(b"not a png")
    data = _ok(reg, "knowledge.assets", {"project_id": "game", "kind": "unreadable"})
    assert data["total_matching"] == 1
    assert _ok(reg, "knowledge.assets", {"project_id": "game", "kind": "image"})["total_matching"] == 2


def test_background_indexer_picks_up_new_files(env, monkeypatch) -> None:
    reg, proj = env
    from cie import knowledge
    from cie.config import load_config, save_config
    cfg = load_config()
    cfg["knowledge"] = {"background_index": True, "interval_sec": 10}
    save_config(cfg)
    started = knowledge.start_background_indexer()
    assert started["started"] is True
    assert knowledge.start_background_indexer()["started"] is False  # idempotent
    deadline = time.time() + 5
    while knowledge.indexer_status()["passes"] < 1 and time.time() < deadline:
        time.sleep(0.05)
    status = _ok(reg, "knowledge.indexer_status")
    assert status["running"] is True and status["passes"] >= 1 and status["last_error"] is None
    knowledge.stop_background_indexer()


def test_background_indexer_can_be_disabled(env) -> None:
    from cie import knowledge
    from cie.config import load_config, save_config
    cfg = load_config()
    cfg["knowledge"] = {"background_index": False}
    save_config(cfg)
    assert knowledge.start_background_indexer()["started"] is False


def test_record_query_and_close_issue(env) -> None:
    reg, proj = env
    _ok(reg, "knowledge.record", {"project": "game", "kind": "decision", "title": "Use Luau strict mode",
                                   "body": "All new scripts start with --!strict."})
    _ok(reg, "knowledge.record", {"project": "game", "kind": "issue", "title": "Jump sound clips",
                                   "body": "Peak hits 0 dBFS on jump.wav."})
    _ok(reg, "knowledge.record", {"project": "game", "kind": "architecture", "title": "Client/server split"})
    assert reg.run("knowledge.record", {"project": "game", "kind": "decision", "title": "y"})["status"] == "approval_required"
    bad = reg.run("knowledge.record", {"project": "game", "kind": "rumor", "title": "x"}, approved=True)
    assert bad["status"] == "error"
    open_issues = _ok(reg, "knowledge.query", {"project": "game", "kind": "issue", "status": "open"})
    assert [n["title"] for n in open_issues["notes"]] == ["Jump sound clips"]
    _ok(reg, "knowledge.issue_status", {"project": "game", "title": "Jump sound clips", "status": "fixed",
                                         "resolution": "Normalized to -1 dBFS."})
    assert _ok(reg, "knowledge.query", {"project": "game", "kind": "issue", "status": "open"})["count"] == 0
    fixed = _ok(reg, "knowledge.query", {"project": "game", "kind": "issue", "status": "fixed"})["notes"][0]
    assert "Normalized" in fixed["summary"]
    found = _ok(reg, "knowledge.query", {"project": "game", "text": "strict"})
    assert [n["title"] for n in found["notes"]] == ["Use Luau strict mode"]


def test_project_summary_is_compact_and_complete(env) -> None:
    reg, proj = env
    _ok(reg, "knowledge.record", {"project": "game", "kind": "issue", "title": "Crate has no collision"})
    _ok(reg, "storage.snapshot", {"project_id": "game", "name": "before polish"})
    summary = _ok(reg, "knowledge.summary", {"project_id": "game"})
    assert summary["assets_by_kind"] == {"image": 2, "audio": 1, "model": 2}
    assert summary["images_not_power_of_two"] == 1
    assert summary["symbols"] >= 1
    assert summary["open_issues"][0]["title"] == "Crate has no collision"
    assert summary["recent_snapshots"][0]["name"] == "before polish"
    assert len(json.dumps(summary)) < 4000  # compact enough to read in one go
