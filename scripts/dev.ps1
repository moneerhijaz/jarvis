# Launch JARVIS — the no-build console at http://127.0.0.1:8765
# (the backend serves the UI directly; no Node/npm needed).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (Test-Path ".venv312") { . .\.venv312\Scripts\Activate.ps1 }
elseif (Test-Path ".venv") { . .\.venv\Scripts\Activate.ps1 }

Write-Host "Open http://127.0.0.1:8765 once it starts." -ForegroundColor Green
python -m jarvis.main serve
