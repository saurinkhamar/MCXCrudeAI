# Prepare local Python/Node dependencies while preserving an existing backend/.env and .venv.
# Use -SkipFrontend on machines that already manage frontend dependencies separately.
param([switch]$SkipFrontend)
# Resolve the workspace root relative to this script so it works from any current directory.
$root=Split-Path -Parent $PSScriptRoot
Set-Location "$root\backend"
# Reuse the existing virtual environment; only create it when missing.
if (!(Test-Path .venv)) { py -3.13 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Copy placeholders only for a first-time setup; never overwrite local credentials.
if (!(Test-Path .env)) { Copy-Item .env.example .env; Write-Host 'Created backend/.env. Fill local credentials before running.' }
if (!$SkipFrontend) { Set-Location "$root\frontend"; npm install }
