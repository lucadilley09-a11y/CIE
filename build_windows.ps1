$ErrorActionPreference = "Stop"

Write-Host "Building CIE Windows x64 standalone MCPB package..."
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (Test-Path ".venv_build") { Remove-Item -Recurse -Force ".venv_build" }
python -m venv .venv_build
$Python = ".\.venv_build\Scripts\python.exe"

& $Python -m pip install --disable-pip-version-check -r requirements.txt
& $Python -m pip install --disable-pip-version-check -r build-requirements.txt
& $Python -m compileall -q cie mcp_server.py dashboard.py scripts
& $Python -m pytest -q
& $Python scripts/build_standalone.py --output release-packages
Write-Host "Standalone MCPB build completed."
