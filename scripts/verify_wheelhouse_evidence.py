"""Verify per-platform wheelhouse manifests, wheel hashes, and license inventories."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_platform_wheelhouses(
    wheelhouses_root: Path, project_root: Path
) -> dict[str, Any]:
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    direct: dict[str, str] = {}
    for requirement in project.get("dependencies", []):
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^;\s]+)\s*", requirement)
        if not match:
            raise RuntimeError(f"Runtime dependency is not an exact pin: {requirement!r}")
        direct[canonical_name(match.group(1))] = match.group(2)

    manifests = sorted(wheelhouses_root.glob("*/wheelhouse-manifest.json"))
    if len(manifests) != 3:
        raise RuntimeError(
            f"Expected exactly 3 platform wheelhouse manifests, found {len(manifests)}"
        )

    platforms: set[str] = set()
    platform_reports: list[dict[str, Any]] = []
    all_wheels: dict[tuple[str, str], dict[str, Any]] = {}
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        env = manifest.get("build_environment") or {}
        system = str(env.get("system") or "")
        machine = str(env.get("machine") or "")
        if system not in {"Windows", "Linux", "Darwin"}:
            raise RuntimeError(f"Unexpected platform in {manifest_path}: {system!r}")
        if system in platforms:
            raise RuntimeError(f"Duplicate platform wheelhouse: {system}")
        platforms.add(system)
        if manifest.get("project", {}).get("name") != project["name"]:
            raise RuntimeError(f"Wrong project in {manifest_path}")
        if manifest.get("project", {}).get("version") != project["version"]:
            raise RuntimeError(f"Wrong project version in {manifest_path}")

        wheel_dir = manifest_path.parent
        entries = manifest.get("wheels")
        if not isinstance(entries, list) or not entries:
            raise RuntimeError(f"Missing wheel entries in {manifest_path}")
        seen: set[str] = set()
        packages: dict[str, str] = {}
        verified_entries: list[dict[str, Any]] = []
        for entry in entries:
            filename = entry.get("filename")
            name = entry.get("name")
            version = str(entry.get("version") or "")
            expected_hash = str(entry.get("sha256") or "")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".whl"):
                raise RuntimeError(f"Unsafe or invalid wheel filename in {manifest_path}: {filename!r}")
            if filename in seen:
                raise RuntimeError(f"Duplicate wheel filename in {manifest_path}: {filename}")
            seen.add(filename)
            wheel_path = wheel_dir / filename
            if not wheel_path.is_file() or wheel_path.is_symlink():
                raise RuntimeError(f"Missing or unsafe wheel file: {wheel_path}")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise RuntimeError(f"Invalid SHA-256 in manifest for {filename}")
            actual_hash = sha256(wheel_path)
            if actual_hash != expected_hash:
                raise RuntimeError(f"SHA-256 mismatch for {wheel_path}")
            key = canonical_name(str(name))
            if key in packages and packages[key] != version:
                raise RuntimeError(f"Multiple versions of {name} in {system} wheelhouse")
            packages[key] = version
            contents = entry.get("contents") or {}
            license_meta = contents.get("license_metadata") or {}
            license_files = contents.get("license_files") or []
            native = contents.get("native_binaries") or []
            if not (license_meta.get("license") or license_meta.get("license_expression")
                    or license_meta.get("classifiers")):
                raise RuntimeError(f"No license metadata recorded for {name}=={version} ({system})")
            if native and not license_files:
                raise RuntimeError(
                    f"Native wheel {name}=={version} has no bundled license/notice file ({system})"
                )
            verified_entries.append({
                "name": str(name), "version": version, "filename": filename,
                "sha256": actual_hash, "license_metadata": license_meta,
                "license_files": license_files, "native_binaries": native,
            })
            all_wheels[(key, version)] = {
                "name": str(name), "version": version, "filename": filename,
                "sha256": actual_hash, "license_metadata": license_meta,
                "license_files": license_files, "native_binaries": native,
            }

        for name, version in direct.items():
            if packages.get(name) != version:
                raise RuntimeError(
                    f"{system} wheelhouse missing exact direct pin {name}=={version}; "
                    f"found {packages.get(name)!r}"
                )
        project_key = canonical_name(project["name"])
        if packages.get(project_key) != project["version"]:
            raise RuntimeError(f"{system} wheelhouse missing the CIE project wheel")
        platform_reports.append({
            "system": system, "machine": machine,
            "python": env.get("python"), "wheel_count": len(entries),
            "wheels": verified_entries,
        })

    if platforms != {"Windows", "Linux", "Darwin"}:
        raise RuntimeError(f"Platform coverage incomplete: {sorted(platforms)}")

    # Conditional-license component must carry its own license text in every target wheel.
    certifi = [w for w in all_wheels.values() if canonical_name(w["name"]) == "certifi"]
    if not certifi:
        raise RuntimeError("certifi is absent from the platform wheelhouse closure")
    for wheel in certifi:
        license_paths = [str(item.get("path", "")).lower() for item in wheel["license_files"]]
        if not any(path.endswith("license") or path.endswith("license.txt") or "/license." in path
                   for path in license_paths):
            raise RuntimeError("certifi wheel is missing its MPL license text")

    return {
        "schema_version": 1,
        "project": {"name": project["name"], "version": project["version"]},
        "direct_pins": direct,
        "platform_count": len(platform_reports),
        "platforms": platform_reports,
        "conditional_license_components": certifi,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouses", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = verify_platform_wheelhouses(args.wheelhouses.resolve(), args.project_root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(
        f"Verified {evidence['platform_count']} platform wheelhouses and "
        f"{sum(p['wheel_count'] for p in evidence['platforms'])} wheel artifacts."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        print(f"Wheelhouse evidence verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
