"""Verify per-platform wheelhouse manifests, wheel hashes, and license inventories."""
from __future__ import annotations

import argparse
import hashlib
import functools
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
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


@functools.lru_cache(maxsize=None)
def pypi_release_evidence(name: str, version: str) -> dict[str, Any]:
    """Compare wheel bytes to PyPI's published SHA-256 and capture source metadata."""
    quoted = urllib.parse.quote(name, safe="")
    quoted_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{quoted}/{quoted_version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "CIE-Phase0-Evidence/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to verify {name}=={version} against PyPI JSON: {exc}") from exc
    info = payload.get("info") or {}
    files = payload.get("urls") or []
    wheel_hashes = {
        item.get("filename"): (item.get("digests") or {}).get("sha256")
        for item in files if item.get("filename", "").endswith(".whl")
    }
    sdists = [
        {"filename": item.get("filename"), "sha256": (item.get("digests") or {}).get("sha256")}
        for item in files if item.get("packagetype") == "sdist"
    ]
    return {
        "pypi_url": url,
        "pypi_wheel_hashes": wheel_hashes,
        "sdists": sdists,
        "home_page": info.get("home_page"),
        "project_urls": info.get("project_urls") or {},
        "license_expression": info.get("license_expression"),
        "license": info.get("license"),
    }


ALL_PLATFORMS = ("Windows", "Linux", "Darwin")


def verify_platform_wheelhouses(
    wheelhouses_root: Path, project_root: Path, required_platforms: tuple[str, ...] = ALL_PLATFORMS
) -> dict[str, Any]:
    required = set(required_platforms)
    if not required or not required <= set(ALL_PLATFORMS):
        raise RuntimeError(f"Unsupported platform selection: {sorted(required_platforms)}")
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    registry_data = yaml.safe_load((project_root / "deps" / "registry.yaml").read_text(encoding="utf-8"))
    registry = {
        canonical_name(str(item["name"])): item
        for item in registry_data.get("packages", [])
        if isinstance(item, dict) and item.get("name")
    }
    policy = registry_data.get("policy") or {}
    allowed_licenses = set(policy.get("tier1_license_allowlist") or [])
    conditional_licenses = set(policy.get("conditional_tier1_licenses") or [])
    aliases = {
        "mit license": "MIT",
        "mit": "MIT",
        "mit-cmu": "MIT-CMU",
        "mit-0": "MIT-0",
        "bsd license": "BSD-3-Clause",
        "bsd-3-clause": "BSD-3-Clause",
        "bsd 3-clause license": "BSD-3-Clause",
        "bsd-2-clause": "BSD-2-Clause",
        "apache software license": "Apache-2.0",
        "apache license 2.0": "Apache-2.0",
        "apache-2.0": "Apache-2.0",
        "apache-2.0 or bsd-3-clause": "Apache-2.0 OR BSD-3-Clause",
        "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
        "psf-2.0": "PSF-2.0",
        "python software foundation license": "PSF-2.0",
        "mit license (mit)": "MIT",
    }
    direct: dict[str, str] = {}
    for requirement in project.get("dependencies", []):
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^;\s]+)\s*", requirement)
        if not match:
            raise RuntimeError(f"Runtime dependency is not an exact pin: {requirement!r}")
        direct[canonical_name(match.group(1))] = match.group(2)

    manifests = sorted(wheelhouses_root.glob("*/wheelhouse-manifest.json"))
    if len(manifests) != len(required):
        raise RuntimeError(
            f"Expected exactly {len(required)} platform wheelhouse manifests, found {len(manifests)}"
        )

    platforms: set[str] = set()
    platform_reports: list[dict[str, Any]] = []
    all_wheels: dict[tuple[str, str, str], dict[str, Any]] = {}
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        env = manifest.get("build_environment") or {}
        system = str(env.get("system") or "")
        machine = str(env.get("machine") or "")
        if system not in required:
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
            pypi = None
            if key != canonical_name(project["name"]):
                pypi = pypi_release_evidence(str(name), version)
                published_hash = pypi["pypi_wheel_hashes"].get(filename)
                if published_hash is None:
                    raise RuntimeError(f"{filename} is not listed in PyPI release metadata")
                if published_hash != actual_hash:
                    raise RuntimeError(f"Wheel hash does not match PyPI for {filename}")
            if key in packages and packages[key] != version:
                raise RuntimeError(f"Multiple versions of {name} in {system} wheelhouse")
            packages[key] = version
            contents = entry.get("contents") or {}
            license_meta = contents.get("license_metadata") or {}
            license_files = contents.get("license_files") or []
            native = contents.get("native_binaries") or []
            # Classifiers provide the concise SPDX-like category when the legacy
            # License field contains an entire bundled license document.
            raw_license = ""
            if license_meta.get("classifiers"):
                raw_license = str(license_meta["classifiers"][0]).split(" :: ")[-1].strip()
            if not raw_license:
                raw_license = str(license_meta.get("license_expression") or license_meta.get("license") or "").strip()
            normalized_license = aliases.get(raw_license.lower(), raw_license)
            if not normalized_license:
                raise RuntimeError(f"No license metadata recorded for {name}=={version} ({system})")
            if normalized_license not in allowed_licenses and normalized_license not in conditional_licenses:
                raise RuntimeError(
                    f"License is not allowlisted for {name}=={version} ({system}): {normalized_license}"
                )
            if normalized_license in conditional_licenses:
                registry_entry = registry.get(key)
                if registry_entry is None:
                    raise RuntimeError(f"Conditional-license package {name} is missing from deps/registry.yaml")
                if str(registry_entry.get("version") or "") != version:
                    raise RuntimeError(f"Conditional-license package {name} version differs from deps/registry.yaml")
                # Approval remains a separate fail-closed release gate; this job gathers
                # evidence even while a human/legal review is pending.
            if native and not license_files:
                raise RuntimeError(
                    f"Native wheel {name}=={version} has no bundled license/notice file ({system})"
                )
            embedded_components = contents.get("embedded_components") or []
            for component in embedded_components:
                component_key = canonical_name(str(component.get("name") or ""))
                component_license = str(component.get("license") or "")
                registry_component = registry.get(component_key)
                if registry_component is None:
                    raise RuntimeError(
                        f"Embedded component {component.get('name')} from {name} is missing from deps/registry.yaml"
                    )
                approved_license_labels = {
                    str(registry_component.get("license") or ""),
                    *(str(value) for value in registry_component.get("license_variants", [])),
                }
                if component_license not in approved_license_labels:
                    raise RuntimeError(
                        f"Embedded component {component.get('name')} license differs from deps/registry.yaml: "
                        f"{component_license}"
                    )
            verified_entries.append({
                "name": str(name), "version": version, "filename": filename,
                "sha256": actual_hash, "license_metadata": license_meta,
                "normalized_license": normalized_license,
                "registry_release_approved": (
                    registry.get(key, {}).get("release_approved")
                    if normalized_license in conditional_licenses else None
                ),
                "license_files": license_files, "native_binaries": native,
                "embedded_components": embedded_components,
                "pypi": pypi,
            })
            all_wheels[(system, key, version)] = {
                "name": str(name), "version": version, "filename": filename,
                "sha256": actual_hash, "license_metadata": license_meta,
                "normalized_license": normalized_license,
                "registry_release_approved": (
                    registry.get(key, {}).get("release_approved")
                    if normalized_license in conditional_licenses else None
                ),
                "license_files": license_files, "native_binaries": native,
                "embedded_components": embedded_components,
                "pypi": pypi,
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

    if platforms != required:
        raise RuntimeError(f"Platform coverage incomplete: {sorted(platforms)}")

    # Conditional-license component must carry its own license text in every target wheel.
    certifi = [w for w in all_wheels.values() if canonical_name(w["name"]) == "certifi"]
    if len(certifi) != len(required):
        raise RuntimeError("certifi must be present in every required platform wheelhouse")
    for wheel in certifi:
        license_paths = [str(item.get("path", "")).lower() for item in wheel["license_files"]]
        if not any(path.endswith("license") or path.endswith("license.txt") or "/license." in path
                   for path in license_paths):
            raise RuntimeError("certifi wheel is missing its MPL license text")

    embedded_sbom: dict[tuple[str, str], dict[str, Any]] = {}
    for wheel in all_wheels.values():
        for component in wheel.get("embedded_components", []):
            component_name = str(component.get("name") or "")
            component_license = str(component.get("license") or "")
            key = (canonical_name(component_name), component_license)
            record = embedded_sbom.setdefault(key, {
                "type": "library",
                "bom-ref": f"urn:cie:embedded:{canonical_name(component_name)}:{component_license}",
                "name": component_name,
                "licenses": [{"license": {"name": component_license}}],
                "properties": [
                    {"name": "cie:source-url", "value": str(component.get("source_url") or "")},
                    {"name": "cie:declared-files", "value": str(component.get("declared_files") or "")},
                ],
            })
            record["properties"].append({
                "name": "cie:bundled-by",
                "value": f"{wheel['name']}=={wheel['version']} ({wheel['filename']})",
            })

    sbom_components: list[dict[str, Any]] = []
    for wheel in sorted(all_wheels.values(), key=lambda item: (item["name"].lower(), item["version"], item["filename"])):
        metadata = wheel["license_metadata"]
        license_value = wheel.get("normalized_license") or metadata.get("license_expression") or metadata.get("license") or "NOASSERTION"
        sbom_components.append({
            "type": "library",
            "name": wheel["name"],
            "version": wheel["version"],
            "purl": f"pkg:pypi/{canonical_name(wheel['name'])}@{wheel['version']}",
            "hashes": [{"alg": "SHA-256", "content": wheel["sha256"]}],
            "licenses": [{"license": {"name": license_value}}],
            "properties": [
                {"name": "cie:platform-wheel", "value": wheel["filename"]},
                {"name": "cie:license-file-count", "value": str(len(wheel["license_files"]))},
                {"name": "cie:native-binary-count", "value": str(len(wheel["native_binaries"]))},
            ],
        })
    sbom_components.extend(
        embedded_sbom[key] for key in sorted(embedded_sbom)
    )
    return {
        "schema_version": 1,
        "project": {"name": project["name"], "version": project["version"]},
        "direct_pins": direct,
        "platform_count": len(platform_reports),
        "platforms": platform_reports,
        "conditional_license_components": certifi,
        "sbom": {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "version": 1,
            "metadata": {"component": {
                "type": "application", "name": project["name"], "version": project["version"],
                "licenses": [{"license": {"id": "MIT"}}],
            }},
            "components": sbom_components,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouses", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--platforms", nargs="+", choices=ALL_PLATFORMS, default=list(ALL_PLATFORMS),
        help="Platforms that must each have exactly one wheelhouse (default: all three)",
    )
    args = parser.parse_args()
    evidence = verify_platform_wheelhouses(
        args.wheelhouses.resolve(), args.project_root.resolve(), tuple(args.platforms)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    sbom_path = args.output.with_name("sbom-platform-wheelhouses.json")
    sbom_path.write_text(json.dumps(evidence["sbom"], indent=2) + "\n", encoding="utf-8")
    print(f"Generated platform-complete CycloneDX SBOM: {sbom_path}")
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
