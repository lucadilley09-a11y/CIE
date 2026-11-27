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
        license_value = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or ""
        ).strip()
        if not license_value or license_value.upper() in {"UNKNOWN", "UNKNOWN LICENSE"}:
            classifiers = [
                value.rsplit(" :: ", 1)[-1]
                for value in distribution.metadata.get_all("Classifier", [])
                if value.startswith("License ::")
            ]
            license_value = classifiers[0] if classifiers else "NOASSERTION"
        manifest_entries.append({
            "name": name,
            "version": version,
            "license": license_value,
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




def write_runtime_sbom(stage: Path, runtime_manifest: dict[str, Any]) -> dict[str, Any]:
    """Generate a CycloneDX SBOM from the actual bundled runtime closure and native notices."""
    project_name = str(runtime_manifest.get("project") or "creative-intelligence-engine")
    project_version = "2.3.2.dev0"
    root_ref = f"pkg:pypi/{canonical_name(project_name)}@{project_version}"
    components: list[dict[str, Any]] = []
    runtime_refs: list[str] = []
    embedded: dict[tuple[str, str], dict[str, Any]] = {}
    component_native_refs: dict[str, set[str]] = {}

    notice_pattern = re.compile(
        r"(?m)^Name:\s*(?P<name>[^\r\n]+)\s*\r?\n"
        r"Files:\s*(?P<files>[^\r\n]+)\s*\r?\n"
        r"(?:Description:\s*(?P<description>[^\r\n]+)\s*\r?\n)?"
        r"Availability:\s*(?P<url>[^\r\n]+)\s*\r?\n"
        r"License:\s*(?P<license>[^\r\n]+)"
    )

    for distribution in runtime_manifest.get("distributions", []):
        name = str(distribution["name"])
        version = str(distribution["version"])
        license_value = str(distribution.get("license") or "NOASSERTION")
        ref = f"pkg:pypi/{canonical_name(name)}@{version}"
        runtime_refs.append(ref)
        components.append({
            "type": "library",
            "bom-ref": ref,
            "name": name,
            "version": version,
            "purl": ref,
            "licenses": [{"license": {"name": license_value}}],
            "properties": [{
                "name": "cie:license-file-count",
                "value": str(len(distribution.get("license_files") or [])),
            }],
        })
        native_refs: set[str] = set()
        for license_file in distribution.get("license_files") or []:
            relative_path = str(license_file.get("path") or "")
            notice_path = stage / Path(*PurePosixPath(relative_path).parts)
            if not notice_path.is_file():
                continue
            notice_text = notice_path.read_text(encoding="utf-8", errors="replace")
            for match in notice_pattern.finditer(notice_text):
                native_name = match.group("name").strip()
                native_license = match.group("license").strip()
                native_key = (canonical_name(native_name), native_license)
                native_ref = f"urn:cie:embedded:{native_key[0]}:{native_license}"
                native_refs.add(native_ref)
                native_record = embedded.setdefault(native_key, {
                    "type": "library",
                    "bom-ref": native_ref,
                    "name": native_name,
                    "licenses": [{"license": {"name": native_license}}],
                    "properties": [
                        {"name": "cie:source-url", "value": match.group("url").strip()},
                        {"name": "cie:declared-files", "value": match.group("files").strip()},
                    ],
                })
                native_record["properties"].append({
                    "name": "cie:bundled-by",
                    "value": f"{name}=={version}",
                })
        if native_refs:
            component_native_refs[ref] = native_refs

    components.extend(embedded[key] for key in sorted(embedded))
    dependencies = [{"ref": root_ref, "dependsOn": sorted(runtime_refs)}]
    dependencies.extend(
        {"ref": ref, "dependsOn": sorted(native_refs)}
        for ref, native_refs in sorted(component_native_refs.items())
    )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": project_name,
                "version": project_version,
                "licenses": [{"license": {"id": "MIT"}}],
                "properties": [{
                    "name": "cie:sbom-status",
                    "value": "generated-from-actual-installed-runtime-closure-and-embedded-native-license-notices",
                }],
            }
        },
        "components": components,
        "dependencies": dependencies,
    }
    (stage / "sbom.json").write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    return sbom


def collect_build_tool_license_files(stage: Path, distribution_name: str) -> dict[str, Any]:
    """Preserve the PyInstaller bootloader license and exception with the binary."""
    distribution = importlib_metadata.distribution(distribution_name)
    name = str(distribution.metadata.get("Name") or distribution_name)
    version = str(distribution.version)
    root = stage / "THIRD_PARTY_LICENSES" / "BUILD_TOOLS" / f"{name}-{version}"
    found: list[dict[str, Any]] = []
    for item in distribution.files or []:
        relative = PurePosixPath(str(item).replace("\\\\", "/"))
        leaf = relative.name.lower()
        if not (leaf.startswith(("license", "copying", "notice", "copyright"))
                or any(part.lower() == "licenses" for part in relative.parts)):
            continue
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe build-tool license path for {name}: {relative}")
        source = Path(distribution.locate_file(item))
        if not source.is_file():
            continue
        destination = root / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        found.append({
            "path": destination.relative_to(stage).as_posix(),
            "size_bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        })
    if not found:
        raise RuntimeError(f"No license/exception files found for embedded build tool {name}=={version}")
    return {"name": name, "version": version, "license_files": sorted(found, key=lambda item: item["path"])}


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
    # including license and notice files for the remaining runtime dependency closure.
    runtime_license_manifest = collect_runtime_license_files(stage, "creative-intelligence-engine")
    write_runtime_sbom(stage, runtime_license_manifest)
    collect_build_tool_license_files(stage, "pyinstaller")
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
