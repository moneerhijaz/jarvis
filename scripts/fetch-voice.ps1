# One-time online download of the voice models so JARVIS can run fully offline.
# Run this ONCE with internet; afterwards voice.offline: true keeps it network-free.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (Test-Path ".venv312") { . .\.venv312\Scripts\Activate.ps1 }
elseif (Test-Path ".venv") { . .\.venv\Scripts\Activate.ps1 }

python scripts\fetch_voice.py
