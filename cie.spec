# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["mcp_server.py"],
    pathex=["."],
    binaries=[],
    datas=[("manifest.json", "."), ("sbom.json", ".")],
    hiddenimports=["cie.config", "cie.registry", "cie.storage", "cie.jobs", "cie.diagnostics", "cie.art", "cie.audio", "cie.coding", "cie.vault", "cie.efficiency", "cie.models", "cie.workflows", "cie.external", "cie.creative", "trimesh", "tree_sitter", "mcp.server.fastmcp"],
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
