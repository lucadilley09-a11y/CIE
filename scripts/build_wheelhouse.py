"""Build a platform-specific offline wheelhouse and SHA-256 manifest.

Run once on each target OS/architecture in a connected build environment. The
resulting directory can be copied to a network-disabled installation target.
All runtime dependencies come from the exact direct pins in pyproject.toml;
transitive resolutions are captured in wheelhouse-manifest.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ is required
    import tomli as tomllib  # type: ignore[no-redef]


def canonical_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def run(args: list[str], cwd: Path) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def wheel_metadata(wheel_path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise RuntimeError(f"Expected one METADATA file in {wheel_path.name}, found {len(metadata_files)}")
        metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8", errors="replace"))
        name, version = metadata.get("Name"), metadata.get("Version")
        if not name or not version:
            raise RuntimeError(f"Wheel metadata missing Name or Version: {wheel_path.name}")
        return name, version




def wheel_contents_inventory(wheel_path: Path) -> dict[str, Any]:
    """Record license metadata/files and native library payloads in a wheel."""
    license_files: list[dict[str, Any]] = []
    native_binaries: list[dict[str, Any]] = []
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise RuntimeError(f"Expected one METADATA file in {wheel_path.name}")
        metadata_text = archive.read(metadata_files[0]).decode("utf-8", errors="replace")
        metadata = Parser().parsestr(metadata_text)
        seen_paths: set[str] = set()
        for info in archive.infolist():
            name = info.filename
            normalized = name.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            if (normalized.startswith("/") or ".." in parts or "\\" in name
                    or (parts and parts[0].endswith(":"))):
                raise RuntimeError(f"Unsafe wheel member path in {wheel_path.name}: {name!r}")
            if name in seen_paths:
                raise RuntimeError(f"Duplicate wheel member in {wheel_path.name}: {name!r}")
            seen_paths.add(name)
            if info.is_dir():
                continue
            leaf = Path(name).name.lower()
            suffix = Path(name).suffix.lower()
            if leaf in {"license", "copying", "notice", "copyright", "license.txt", "copying.txt", "notice.txt"} or leaf.startswith(("license.", "copying.", "notice.")):
                payload = archive.read(info)
                license_files.append({
                    "path": name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            if suffix in {".so", ".dll", ".dylib", ".pyd"}:
                payload = archive.read(info)
                native_binaries.append({
                    "path": name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
    return {
        "license_metadata": {
            "license_expression": metadata.get("License-Expression", ""),
            "license": metadata.get("License", ""),
            "classifiers": [value for value in metadata.get_all("Classifier", []) if value.startswith("License ::")],
        },
        "license_files": sorted(license_files, key=lambda item: item["path"]),
        "native_binaries": sorted(native_binaries, key=lambda item: item["path"]),
    }

def read_exact_requirements(path: Path) -> dict[str, str]:
    """Read a deliberately small, exact-pin requirements file."""
    import re

    requirements: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", line)
        if not match:
            raise RuntimeError(f"Build-system requirement is not an exact pin in {path}: {line!r}")
        requirements[canonical_name(match.group(1))] = match.group(2)
    if not requirements:
        raise RuntimeError(f"No build-system requirements found in {path}")
    return requirements


def verify_build_system(
    requirements: dict[str, str], python_executable: str = sys.executable
) -> dict[str, str]:
    """Fail before touching output if the selected build interpreter is not pinned."""
    probe = (
        "import importlib.metadata as m, json, sys; "
        "names=json.loads(sys.argv[1]); "
        "result={}; "
        "\nfor name in names:\n"
        " try: result[name]=m.version(name)\n"
        " except m.PackageNotFoundError: result[name]='NOT INSTALLED'\n"
        "print(json.dumps(result, sort_keys=True))"
    )
    completed = subprocess.run(
        [python_executable, "-c", probe, json.dumps(list(requirements))],
        check=True, capture_output=True, text=True,
    )
    actual = json.loads(completed.stdout)
    mismatches = [
        f"{name}: expected {expected}, found {actual.get(name, 'NOT INSTALLED')}"
        for name, expected in requirements.items()
        if actual.get(name) != expected
    ]
    if mismatches:
        raise RuntimeError(
            "Build environment does not match build-system-requirements.txt. "
            "Install the pinned build tools from a local build-tools wheelhouse, then retry. "
            + "; ".join(mismatches)
        )
    return actual


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def prepare_output_directory(output: Path, project_root: Path) -> None:
    """Create or safely recycle a dedicated wheelhouse directory.

    Never delete arbitrary archives or files supplied by the caller. A non-empty
    directory is recyclable only when its previous manifest accounts for every
    entry and every previous wheel still matches its recorded digest.
    """
    if output == project_root or output in project_root.parents:
        raise RuntimeError("Wheelhouse output must not be the project root or its parent")
    if output.is_symlink():
        raise RuntimeError(f"Refusing symlink wheelhouse output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise RuntimeError(f"Wheelhouse output is not a directory: {output}")
    children = list(output.iterdir())
    if not children:
        return

    manifest_path = output / "wheelhouse-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError(
            f"Refusing to overwrite non-empty output directory without a valid prior manifest: {output}"
        )
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot safely recycle output directory; invalid manifest: {exc}") from exc
    previous_entries = previous.get("wheels") if isinstance(previous, dict) else None
    if not isinstance(previous_entries, list):
        raise RuntimeError("Cannot safely recycle output directory; prior manifest has no wheel list")

    expected: dict[str, str] = {}
    for entry in previous_entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Cannot safely recycle output directory; malformed prior wheel entry")
        filename, digest = entry.get("filename"), entry.get("sha256")
        if (not isinstance(filename, str) or not filename.endswith(".whl")
                or Path(filename).name != filename or not isinstance(digest, str)):
            raise RuntimeError("Cannot safely recycle output directory; unsafe prior wheel entry")
        if filename in expected:
            raise RuntimeError(f"Duplicate wheel entry in prior manifest: {filename}")
        expected[filename] = digest

    actual_names = {child.name for child in children}
    allowed_names = set(expected) | {manifest_path.name}
    if actual_names != allowed_names:
        raise RuntimeError(
            "Refusing to delete untracked wheelhouse contents; "
            f"untracked={sorted(actual_names - allowed_names)}, "
            f"missing={sorted(allowed_names - actual_names)}"
        )
    for filename, digest in expected.items():
        wheel = output / filename
        if wheel.is_symlink() or not wheel.is_file() or sha256(wheel) != digest:
            raise RuntimeError(f"Refusing to delete wheel not matching previous manifest: {filename}")
    for filename in expected:
        (output / filename).unlink()
    manifest_path.unlink()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("wheelhouse"))
    parser.add_argument("--python", default=sys.executable, help="Python executable used for pip wheel")
    ns = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output_arg = ns.output if ns.output.is_absolute() else root / ns.output
    if output_arg.is_symlink():
        raise RuntimeError(f"Refusing symlink wheelhouse output directory: {output_arg}")
    output = output_arg.resolve()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project") or {}
    project_name = str(project.get("name") or "")
    project_version = str(project.get("version") or "")
    direct_pins: dict[str, str] = {}
    for requirement in project.get("dependencies") or []:
        import re
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^;\s]+)\s*", requirement)
        if not match:
            raise RuntimeError(f"Runtime dependency is not an exact pin: {requirement!r}")
        direct_pins[canonical_name(match.group(1))] = match.group(2)

    build_requirements = read_exact_requirements(root / "build-system-requirements.txt")
    build_environment = verify_build_system(build_requirements, ns.python)
    prepare_output_directory(output, root)

    run([
        ns.python, "-m", "pip", "wheel",
        "--disable-pip-version-check",
        "--no-build-isolation",
        "--only-binary=:all:",
        "--wheel-dir", str(output),
        str(root),
    ], root)

    wheel_files = sorted(output.glob("*.whl"))
    if not wheel_files:
        raise RuntimeError("pip wheel completed without producing any wheel files")
    entries: list[dict[str, Any]] = []
    by_name: dict[str, str] = {}
    for wheel in wheel_files:
        name, version = wheel_metadata(wheel)
        key = canonical_name(name)
        if key in by_name and by_name[key] != version:
            raise RuntimeError(f"Multiple wheel versions for {name}: {by_name[key]} and {version}")
        by_name[key] = version
        entries.append({
            "name": name,
            "version": version,
            "filename": wheel.name,
            "size_bytes": wheel.stat().st_size,
            "sha256": sha256(wheel),
            "contents": wheel_contents_inventory(wheel),
        })

    missing: list[str] = []
    wrong: list[str] = []
    for name, expected in direct_pins.items():
        actual = by_name.get(name)
        if actual is None:
            missing.append(name)
        elif actual != expected:
            wrong.append(f"{name}: expected {expected}, got {actual}")
    if missing or wrong:
        raise RuntimeError(
            "Wheelhouse does not contain the pinned direct dependency set. "
            f"Missing={missing}; version mismatches={wrong}"
        )
    expected_project = canonical_name(project_name)
    if by_name.get(expected_project) != project_version:
        raise RuntimeError(
            f"Wheelhouse missing {project_name}=={project_version}; found {by_name.get(expected_project)!r}"
        )

    manifest = {
        "schema_version": 1,
        "project": {"name": project_name, "version": project_version},
        "build_system_requirements": build_requirements,
        "verified_build_system_environment": build_environment,
        "build_environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "platform": sys.platform,
        },
        "direct_pins": direct_pins,
        "wheel_count": len(entries),
        "wheels": entries,
        "offline_install_command": "python scripts/verify_offline_install.py <wheelhouse-directory>",
    }
    manifest_path = output / "wheelhouse-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Built {len(entries)} wheels for {platform.system()} {platform.machine()} at {output}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Wheelhouse build failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
