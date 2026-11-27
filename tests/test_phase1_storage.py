"""Phase 1.3 storage tests: dedupe, compression, preview-first rollback,
safety snapshots, retention, auto-snapshot before writes, reports, archives."""
from __future__ import annotations

import json
import zlib
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
    (proj / "assets").mkdir(parents=True)
    (proj / "main.lua").write_text("print('v1')\n" * 200, encoding="utf-8")
    (proj / "assets" / "big.bin").write_bytes(bytes(range(256)) * 4000)
    from cie.projects import register_project
    register_project(str(proj), "game")
    yield reg, proj, home
    storage._DEFAULT_STORAGE = None


def _run(reg, cid, args=None, approved=False):
    res = reg.run(cid, args or {}, approved=approved)
    assert res["status"] == "ok", res
    return res


def test_snapshots_dedupe_and_compress(env) -> None:
    reg, proj, home = env
    s1 = _run(reg, "storage.snapshot", {"project_id": "game", "name": "baseline"})["data"]
    assert s1["files_saved"] == 2 and s1["new_blobs"] == 2
    s2 = _run(reg, "storage.snapshot", {"project_id": "game"})["data"]
    assert s2["new_blobs"] == 0 and s2["new_stored_bytes"] == 0  # unchanged files cost nothing
    report = _run(reg, "storage.report")["data"]
    store = report["store"]
    assert store["unique_blobs"] == 2
    assert store["stored_bytes"] < store["unique_bytes"]  # zlib helped on repetitive content
    assert store["saved_percent"] > 50


def test_restore_preview_then_restore_with_safety_snapshot(env) -> None:
    reg, proj, _ = env
    snap = _run(reg, "storage.snapshot", {"paths": [str(proj / "main.lua"), str(proj / "new.lua")]})["data"]
    assert snap["files_absent"] == 1
    (proj / "main.lua").write_text("broken\n", encoding="utf-8")
    (proj / "new.lua").write_text("created later\n", encoding="utf-8")
    preview = _run(reg, "storage.restore_preview", {"snapshot_id": snap["snapshot_id"]})["data"]
    assert preview["will_overwrite"] == [str(proj / "main.lua")]
    assert preview["will_delete"] == [str(proj / "new.lua")]
    assert (proj / "main.lua").read_text() == "broken\n"  # preview changed nothing
    assert reg.run("storage.restore", {"snapshot_id": snap["snapshot_id"]})["status"] == "approval_required"
    done = _run(reg, "storage.restore", {"snapshot_id": snap["snapshot_id"]}, approved=True)["data"]
    assert (proj / "main.lua").read_text().startswith("print('v1')")
    assert not (proj / "new.lua").exists()
    # The rollback itself can be undone from its safety snapshot.
    _run(reg, "storage.restore", {"snapshot_id": done["safety_snapshot_id"]}, approved=True)
    assert (proj / "main.lua").read_text() == "broken\n"
    assert (proj / "new.lua").read_text() == "created later\n"


def test_project_restore_keeps_added_files_unless_asked(env) -> None:
    reg, proj, _ = env
    snap = _run(reg, "storage.snapshot", {"project_id": "game"})["data"]["snapshot_id"]
    (proj / "extra.txt").write_text("mine", encoding="utf-8")
    preview = _run(reg, "storage.restore_preview", {"snapshot_id": snap})["data"]
    assert str(proj / "extra.txt") in preview["added_since_snapshot"] and not preview["will_delete"]
    preview2 = _run(reg, "storage.restore_preview", {"snapshot_id": snap, "remove_added": True})["data"]
    assert str(proj / "extra.txt") in preview2["will_delete"]


def test_corrupt_blob_is_never_restored(env) -> None:
    reg, proj, home = env
    snap = _run(reg, "storage.snapshot", {"paths": [str(proj / "main.lua")]})["data"]["snapshot_id"]
    from cie.snapstore import _blob_path, _entries
    sha = _entries(snap)[0]["sha256"]
    _blob_path(sha).write_bytes(zlib.compress(b"tampered"))
    (proj / "main.lua").write_text("current\n", encoding="utf-8")
    res = _run(reg, "storage.restore", {"snapshot_id": snap}, approved=True)["data"]
    assert res["restored"] == 0 and any("integrity" in e for e in res["errors"])
    assert (proj / "main.lua").read_text() == "current\n"


def test_auto_snapshot_before_a_write_capability(env) -> None:
    reg, proj, _ = env
    out = proj / "sfx.wav"
    res = reg.run("audio.procedural_sfx", {"output_path": str(out), "duration_sec": 0.05}, approved=True)
    assert res["status"] == "ok", res
    snaps = [e for e in res["evidence"] if isinstance(e, dict) and "auto_snapshot" in e]
    assert len(snaps) == 1
    sid = snaps[0]["auto_snapshot"]
    preview = _run(reg, "storage.restore_preview", {"snapshot_id": sid})["data"]
    assert preview["will_delete"] == [str(out)]  # file did not exist before the write
    listed = _run(reg, "storage.snapshots", {"project_id": "game"})["data"]
    assert any(s["snapshot_id"] == sid and s["kind"] == "auto" for s in listed["snapshots"])


def test_auto_snapshot_can_be_disabled(env) -> None:
    reg, proj, _ = env
    from cie.config import load_config, save_config
    cfg = load_config()
    cfg["storage"] = {"auto_snapshot": False}
    save_config(cfg)
    res = reg.run("audio.procedural_sfx", {"output_path": str(proj / "a.wav"), "duration_sec": 0.05}, approved=True)
    assert res["status"] == "ok"
    assert not [e for e in res["evidence"] if isinstance(e, dict) and "auto_snapshot" in e]


def test_retention_keeps_named_and_recent(env) -> None:
    reg, proj, _ = env
    from cie.storage import get_storage
    old = _run(reg, "storage.snapshot", {"paths": [str(proj / "main.lua")]})["data"]["snapshot_id"]
    named = _run(reg, "storage.snapshot", {"paths": [str(proj / "assets" / "big.bin")], "name": "keep me"})["data"]["snapshot_id"]
    recent = _run(reg, "storage.snapshot", {"paths": [str(proj / "main.lua")]})["data"]["snapshot_id"]
    get_storage().execute("UPDATE snapshots SET created_at = '2020-01-01T00:00:00+00:00' WHERE snapshot_id IN (?, ?)", (old, named))
    dry = _run(reg, "storage.prune")["data"]
    assert dry["dry_run"] and dry["snapshots_to_remove"] == 1
    assert reg.run("storage.prune", {"dry_run": False})["status"] == "error"
    assert reg.run("storage.prune_apply", {})["status"] == "approval_required"
    _run(reg, "storage.prune_apply", {}, approved=True)
    ids = {s["snapshot_id"] for s in _run(reg, "storage.snapshots", {"limit": 50})["data"]["snapshots"]}
    assert named in ids and recent in ids and old not in ids
    # Blob for main.lua is still referenced by the recent snapshot, so it survives.
    assert _run(reg, "storage.restore_preview", {"snapshot_id": recent})["data"]["unchanged"] == 1


def test_compare_shows_text_diff(env) -> None:
    reg, proj, _ = env
    a = _run(reg, "storage.snapshot", {"paths": [str(proj / "main.lua")]})["data"]["snapshot_id"]
    (proj / "main.lua").write_text("print('v2')\n", encoding="utf-8")
    full = _run(reg, "storage.compare", {"snapshot_a": a, "max_diff_lines": 500})["data"]
    assert full["modified"] == [str(proj / "main.lua")]
    assert any("+print('v2')" in line for line in full["text_diffs"][0]["diff"])
    short = _run(reg, "storage.compare", {"snapshot_a": a, "max_diff_lines": 10})["data"]
    assert len(short["text_diffs"][0]["diff"]) == 10 and short["text_diffs"][0]["truncated"]


def test_duplicate_report_is_read_only(env) -> None:
    reg, proj, _ = env
    (proj / "copy.bin").write_bytes((proj / "assets" / "big.bin").read_bytes())
    rep = _run(reg, "storage.duplicates", {"project_id": "game"})["data"]
    assert rep["duplicate_groups"] == 1 and rep["wasted_bytes"] == (proj / "copy.bin").stat().st_size
    assert (proj / "copy.bin").exists()


def test_png_optimization_is_lossless(env) -> None:
    from PIL import Image
    reg, proj, _ = env
    png = proj / "tile.png"
    img = Image.new("RGBA", (64, 64))
    for x in range(64):
        for y in range(64):
            img.putpixel((x, y), ((x * 4) % 256, (y * 4) % 256, 128, 255 if (x + y) % 3 else 0))
    img.save(png, compress_level=0)
    before_pixels = Image.open(png).convert("RGBA").tobytes()
    report = _run(reg, "storage.optimize_png", {"paths": [str(png)]}, approved=True)["data"]
    assert report["files"][0]["status"] == "smaller" and png.stat().st_size == report["files"][0]["before"]
    applied = reg.run("storage.optimize_png", {"paths": [str(png)], "apply": True}, approved=True)
    assert applied["data"]["files"][0]["status"] == "optimized"
    assert Image.open(png).convert("RGBA").tobytes() == before_pixels
    assert any("auto_snapshot" in e for e in applied["evidence"] if isinstance(e, dict))


def test_archive_roundtrip_never_overwrites(env, tmp_path: Path) -> None:
    reg, proj, _ = env
    tmp_path = tmp_path.resolve()
    arch = _run(reg, "storage.archive", {"project_id": "game"})["data"]
    assert arch["verified"] and arch["files"] == 2
    dest = tmp_path / "restored"
    _run(reg, "storage.archive_restore", {"archive": arch["archive"], "target_dir": str(dest)}, approved=True)
    assert (dest / "main.lua").read_bytes() == (proj / "main.lua").read_bytes()
    again = reg.run("storage.archive_restore", {"archive": arch["archive"], "target_dir": str(dest)}, approved=True)
    assert again["status"] == "error" and "new or empty" in again["summary"]


def test_legacy_snapshot_still_restores(env) -> None:
    reg, proj, home = env
    import hashlib
    from cie.config import get_vault_root
    from cie.storage import get_storage
    blob_dir = get_vault_root() / ".cie" / "snapshots" / "snap_legacy01"
    blob_dir.mkdir(parents=True)
    data = (proj / "main.lua").read_bytes()
    blob = blob_dir / "0_x.bin"
    blob.write_bytes(data)
    manifest = [{"original_path": str(proj / "main.lua"), "project_id": "game",
                 "sha256": hashlib.sha256(data).hexdigest(), "blob_path": str(blob)}]
    get_storage().execute(
        "INSERT INTO snapshots(snapshot_id, project_id, description, manifest_json, created_at) VALUES (?, ?, ?, ?, ?)",
        ("snap_legacy01", "game", "old", json.dumps(manifest), "2026-11-01T00:00:00+00:00"),
    )
    (proj / "main.lua").write_text("changed", encoding="utf-8")
    res = _run(reg, "storage.restore", {"snapshot_id": "snap_legacy01"}, approved=True)["data"]
    assert res["count"] == 1 and (proj / "main.lua").read_bytes() == data
    assert res["safety_snapshot_id"]


def test_parallel_first_use_shares_one_storage_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: parallel steps initializing storage at once used to race
    and fail with 'duplicate column name' during the schema migration."""
    import threading
    monkeypatch.setenv("CIE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("CIE_VAULT", str(tmp_path / "h" / "vault"))
    from cie import storage
    storage._DEFAULT_STORAGE = None
    managers, errors = [], []
    barrier = threading.Barrier(8)

    def grab():
        try:
            barrier.wait()
            managers.append(storage.get_storage())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len({id(m) for m in managers}) == 1
    storage._DEFAULT_STORAGE = None
