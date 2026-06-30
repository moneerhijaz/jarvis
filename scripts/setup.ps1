# JARVIS backend setup (Windows PowerShell).
# Creates a venv, installs the package + dev deps, and runs the test suite.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

Write-Host "`nRunning tests..." -ForegroundColor Cyan
pytest -q

Write-Host "`nDone. Start the backend with:  python -m jarvis.main serve" -ForegroundColor Green
