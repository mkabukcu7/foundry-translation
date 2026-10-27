# SPDX-License-Identifier: MIT
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
foreach ($file in @('.env', '.agent-versions.env')) {
    Get-Content $file | ForEach-Object {
        if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] }
    }
}
if (-not $env:PYTHON_EXECUTABLE) { throw 'Set PYTHON_EXECUTABLE in .env' }
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
& $env:PYTHON_EXECUTABLE agents\calibrate_reviews.py
exit $LASTEXITCODE
