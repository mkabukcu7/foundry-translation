# SPDX-License-Identifier: MIT
param([switch]$TranslatorOnly)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
Get-Content .env | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] }
}
if (Test-Path .agent-versions.env) {
    Get-Content .agent-versions.env | ForEach-Object {
        if ($_ -match '^((?:TERM_GUARD|CHECKER)_VERSION)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] }
    }
}
if (-not $env:PYTHON_EXECUTABLE) { throw 'Set PYTHON_EXECUTABLE in .env to an installed Python environment' }
$env:PYTHONPATH = "$(Get-Location);$(Join-Path (Get-Location) 'src')"
$port = if ($TranslatorOnly) { 8766 } else { 8765 }
$app = if ($TranslatorOnly) { 'scripts.showcase:baseline_app' } else { 'scripts.showcase:app' }
Write-Host "Translation showcase: http://localhost:$port (local access only; Ctrl+C to stop)"
& $env:PYTHON_EXECUTABLE -m uvicorn $app --host 127.0.0.1 --port $port --no-access-log
exit $LASTEXITCODE
