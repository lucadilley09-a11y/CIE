"""Install CIE from a local wheelhouse with package-index access disabled."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], env: dict[str, str], cwd: Path) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, env=env, cwd=cwd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse", type=Path)
    parser.add_argument("--keep-temp", action="store_true", help="Preserve the test environment for inspection")
    ns = parser.parse_args()
    wheelhouse_arg = ns.wheelhouse
    if wheelhouse_arg.is_symlink():
        parser.error("wheelhouse directory must not be a symlink")
    wheelhouse = wheelhouse_arg.resolve()
    manifest_path = wheelhouse / "wheelhouse-manifest.json"
    if not wheelhouse.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        parser.error("wheelhouse must contain wheelhouse-manifest.json and its listed wheels")
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"invalid wheelhouse manifest: {exc}")
    if manifest.get("schema_version") != 1:
        parser.error("unsupported wheelhouse manifest schema")
    wheel_entries = manifest.get("wheels")
    if not isinstance(wheel_entries, list) or not wheel_entries:
        parser.error("wheelhouse manifest contains no wheel entries")

    expected_files: set[str] = set()
    for item in wheel_entries:
        if not isinstance(item, dict):
            parser.error("invalid wheel entry in manifest")
        filename = item.get("filename")
        digest = item.get("sha256")
        if (
            not isinstance(filename, str)
            or not filename.endswith(".whl")
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or (wheelhouse / filename).is_symlink()
        ):
            parser.error(f"unsafe wheel filename in manifest: {filename!r}")
        path = wheelhouse / filename
        if not path.is_file() or path.is_symlink():
            parser.error(f"manifest wheel missing or not a regular file: {filename}")
        expected_size = item.get("size_bytes")
        if isinstance(expected_size, int) and path.stat().st_size != expected_size:
            parser.error(f"size mismatch for {filename}")
        if not isinstance(digest, str) or sha256(path) != digest:
            parser.error(f"SHA-256 mismatch for {filename}")
        expected_files.add(filename)
    actual_files = {path.name for path in wheelhouse.glob("*.whl")}
    unexpected_entries = {
        path.name for path in wheelhouse.iterdir()
        if path.name != manifest_path.name and path.name not in expected_files
    }
    if actual_files != expected_files or unexpected_entries:
        parser.error(
            "wheelhouse file set differs from manifest; "
            f"extra_wheels={sorted(actual_files - expected_files)} "
            f"missing_wheels={sorted(expected_files - actual_files)} "
            f"unexpected_entries={sorted(unexpected_entries)}"
        )

    project = manifest.get("project") or {}
    package_name = str(project.get("name") or "creative-intelligence-engine")
    package_version = str(project.get("version") or "")
    if not package_version:
        parser.error("project version missing from wheelhouse manifest")
    direct_pins = manifest.get("direct_pins")
    if not isinstance(direct_pins, dict) or not direct_pins:
        parser.error("direct_pins missing from wheelhouse manifest")

    temp_root = Path(tempfile.mkdtemp(prefix="cie-offline-install-"))
    try:
        venv_dir = temp_root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
        python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        home = temp_root / "cie-home"
        vault = temp_root / "cie-vault"
        workdir = temp_root / "work"
        workdir.mkdir()
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.pop("VIRTUAL_ENV", None)
        env.update({
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
            "CIE_HOME": str(home),
            "CIE_VAULT": str(vault),
            "PIP_CONFIG_FILE": os.devnull,
        })
        run([
            str(python), "-m", "pip", "install", "--isolated", "--no-index",
            "--find-links", str(wheelhouse), f"{package_name}=={package_version}",
        ], env, workdir)
        run([str(python), "-m", "pip", "check"], env, workdir)
        run([str(python), "-m", "cie", "--version"], env, workdir)
        run([str(python), "-m", "cie", "config"], env, workdir)
        run([str(python), "-m", "cie", "doctor"], env, workdir)
        check_versions = (
            "import importlib.metadata as m; import cie; from mcp.server.fastmcp import FastMCP; "
            f"expected = {direct_pins!r}; "
            "actual = {name: m.version(name) for name in expected}; "
            "mismatches = {name: (expected[name], actual[name]) for name in expected if actual[name] != expected[name]}; "
            "assert not mismatches, mismatches; "
            f"assert m.version({package_name!r}) == {package_version!r}; "
            "assert cie.__version__ == " + repr(package_version) + "; "
            "print('CIE package, exact direct pins, and MCP imports passed')"
        )
        run([str(python), "-c", check_versions], env, workdir)
        print("Offline wheelhouse installation and smoke checks passed.")
        if ns.keep_temp:
            print(f"Preserved test environment: {temp_root}")
        return 0
    finally:
        if not ns.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Offline install verification failed with exit code {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
