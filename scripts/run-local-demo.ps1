# SPDX-License-Identifier: MIT
param(
    [Parameter(Mandatory)][string]$InputFile,
    [string]$SourceLanguage = 'it',
    [string]$TargetLanguage = 'en'
)
$ErrorActionPreference = 'Stop'
Get-Content (Join-Path $PSScriptRoot '..\.env') | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] }
}
if (Test-Path (Join-Path $PSScriptRoot '..\.agent-versions.env')) {
    Get-Content (Join-Path $PSScriptRoot '..\.agent-versions.env') | ForEach-Object {
        if ($_ -match '^((?:TERM_GUARD|CHECKER)_VERSION)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] }
    }
}
if (-not $env:PYTHON_EXECUTABLE) { throw 'Set PYTHON_EXECUTABLE in .env to an installed Python environment' }
$env:PYTHONPATH = Join-Path $PSScriptRoot '..\src'
& $env:PYTHON_EXECUTABLE (Join-Path $PSScriptRoot 'local_demo.py') $InputFile `
    --source-language $SourceLanguage --target-language $TargetLanguage
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
