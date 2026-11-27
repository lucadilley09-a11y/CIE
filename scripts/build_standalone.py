"""Build a platform-native, self-contained CIE MCPB bundle using PyInstaller."""
from __future__ import annotations

import argparse
import json
import hashlib
import importlib.metadata as importlib_metadata
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.version import Version


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def collect_runtime_license_files(stage: Path, project_name: str) -> dict[str, Any]:
    """Copy license/notice files for the installed runtime dependency closure.

    This deliberately fails closed when an installed runtime distribution has
    no license text in its installed file inventory. Registry metadata alone is
    not a substitute for preserving required notices in the standalone bundle.
    """
    distributions = list(importlib_metadata.distributions())
    by_name: dict[str, Any] = {}
    for distribution in distributions:
        name = str(distribution.metadata.get("Name") or "").strip()
        if name:
            by_name[canonical_name(name)] = distribution

    root = by_name.get(canonical_name(project_name))
    if root is None:
        raise RuntimeError(f"Installed project distribution {project_name!r} was not found")

    closure: dict[str, Any] = {}
    processed_contexts: dict[str, set[str]] = {}
    pending: list[tuple[Any, set[str]]] = [(root, set())]
    while pending:
        distribution, requested_extras = pending.pop()
        name = str(distribution.metadata.get("Name") or "").strip()
        key = canonical_name(name)
        if not key:
            continue
        closure[key] = distribution
        processed = processed_contexts.setdefault(key, set())
        contexts = (requested_extras | {""}) - processed
        if not contexts:
            continue
        processed.update(contexts)
        for extra in sorted(contexts):
            marker_environment = default_environment()
            marker_environment["extra"] = extra
            for requirement_text in distribution.requires or []:
                requirement = Requirement(requirement_text)
                if requirement.marker is not None and not requirement.marker.evaluate(marker_environment):
                    continue
                dependency = by_name.get(canonical_name(requirement.name))
                if dependency is None:
                    raise RuntimeError(
                        f"Required runtime dependency {requirement.name} ({requirement_text}) "
                        f"is not installed; refusing to bundle an incomplete runtime."
                    )
                if requirement.specifier and Version(str(dependency.version)) not in requirement.specifier:
                    raise RuntimeError(
                        f"Installed runtime dependency {requirement.name}=={dependency.version} "
                        f"does not satisfy {requirement.specifier}."
                    )
                pending.append((dependency, set(requirement.extras)))

    license_root = stage / "THIRD_PARTY_LICENSES"
    manifest_entries: list[dict[str, Any]] = []
    missing: list[str] = []
    for key, distribution in sorted(closure.items()):
        if key == canonical_name(project_name):
            continue  # CIE's own LICENSE is copied at the MCPB root.
        name = str(distribution.metadata.get("Name") or key)
        version = str(distribution.version)
        package_dir_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{name}-{version}")
        found: list[dict[str, Any]] = []
        for item in distribution.files or []:
            relative_text = str(item).replace("\\", "/")
            relative = PurePosixPath(relative_text)
            leaf = relative.name.lower()
            is_license = (
                leaf.startswith(("license", "copying", "notice", "copyright"))
                or any(part.lower() == "licenses" for part in relative.parts)
            )
            if not is_license:
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError(f"Unsafe installed license path for {name}: {relative_text!r}")
            source = Path(distribution.locate_file(item))
            if not source.is_file():
                continue
            destination = license_root / package_dir_name / Path(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            found.append({
                "path": destination.relative_to(stage).as_posix(),
                "source_path": relative.as_posix(),
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
            })
        if not found:
            missing.append(f"{name}=={version}")
        manifest_entries.append({
            "name": name,
            "version": version,
            "license_files": sorted(found, key=lambda entry: entry["source_path"]),
        })

    if missing:
        raise RuntimeError(
            "Runtime dependencies are missing distributable license/notice files; "
            "refusing to create a bundle: " + ", ".join(missing)
        )
    result = {
        "schema_version": 1,
        "project": project_name,
        "runtime_distribution_count": len(manifest_entries),
        "distributions": manifest_entries,
    }
    (stage / "THIRD_PARTY_LICENSES.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("release-packages"))
    parser.add_argument("--skip-smoke-test", action="store_true")
    ns = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = ns.output if ns.output.is_absolute() else root / ns.output
    output.mkdir(parents=True, exist_ok=True)

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        raise SystemExit("PyInstaller is not installed. Install build-requirements.txt first.") from exc

    os.chdir(root)
    PyInstaller.__main__.run(["--clean", "--noconfirm", str(root / "cie.spec")])
    executable = root / "dist" / ("cie-mcp.exe" if os.name == "nt" else "cie-mcp")
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce the expected executable: {executable}")

    system = platform.system().lower()
    machine = platform.machine().lower().replace(" ", "-")
    platform_id = {"windows": "win32", "darwin": "darwin", "linux": "linux"}.get(system)
    if platform_id is None:
        raise RuntimeError(f"Unsupported standalone packaging platform: {system}")

    if not ns.skip_smoke_test:
        with tempfile.TemporaryDirectory(prefix="cie-standalone-smoke-") as tmp:
            env = os.environ.copy()
            env["CIE_HOME"] = str(Path(tmp) / "home")
            env["CIE_VAULT"] = str(Path(tmp) / "vault")
            subprocess.run([str(executable), "--version"], check=True, env=env)
            subprocess.run([str(executable), "--doctor"], check=True, env=env)

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "2.3.2"
    manifest["compatibility"]["platforms"] = [platform_id]
    command_name = "cie-mcp.exe" if os.name == "nt" else "cie-mcp"
    manifest["server"]["entry_point"] = f"dist/{command_name}"
    manifest["server"]["mcp_config"]["command"] = f"${{__dirname}}/dist/{command_name}"

    stage = output / f"CIE-{platform_id}-{machine}"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "dist").mkdir(parents=True)
    shutil.copy2(executable, stage / "dist" / command_name)
    for filename in ("sbom.json", "LICENSE"):
        shutil.copy2(root / filename, stage / filename)
    # Preserve license/notice files for the installed runtime dependency closure,
    # including SoundFile's bundled libsndfile COPYING notice where present.
    collect_runtime_license_files(stage, "creative-intelligence-engine")
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    build_info: dict[str, Any] = {
        "version": "2.3.2.dev0",
        "platform": platform_id,
        "machine": machine,
        "python": platform.python_version(),
        "packager": "PyInstaller",
        "self_contained_python_runtime": True,
        "smoke_test_executed": not ns.skip_smoke_test,
    }
    (stage / "BUILD_INFO.json").write_text(json.dumps(build_info, indent=2) + "\n", encoding="utf-8")

    archive = output / f"CIE-{platform_id}-{machine}.mcpb"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                bundle.write(file, file.relative_to(stage).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        corrupt = bundle.testzip()
        if corrupt:
            raise RuntimeError(f"Corrupt MCPB archive member: {corrupt}")
    print(f"Created standalone bundle: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
