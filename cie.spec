# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# SoundFile loads libsndfile dynamically. Include the bundled native library
# and its LGPL license text explicitly rather than relying on static analysis.
soundfile_binaries = collect_dynamic_libs("_soundfile_data")
soundfile_license_files = collect_data_files("_soundfile_data", includes=["COPYING"])
if not soundfile_binaries:
    raise RuntimeError("SoundFile's bundled libsndfile binary was not found; refusing an incomplete standalone build.")

a = Analysis(
    ["mcp_server.py"],
    pathex=["."],
    binaries=soundfile_binaries,
    datas=[("manifest.json", "."), ("sbom.json", ".")] + soundfile_license_files,
    hiddenimports=["_soundfile_data", "cie.config", "cie.registry", "cie.storage", "cie.jobs", "cie.diagnostics", "cie.art", "cie.audio", "cie.coding", "cie.vault", "cie.efficiency", "cie.models", "cie.workflows", "cie.external", "cie.creative", "pyloudnorm", "trimesh", "soundfile", "tree_sitter", "mcp.server.fastmcp"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cie-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
