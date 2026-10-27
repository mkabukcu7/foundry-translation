# SPDX-License-Identifier: MIT
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$python = ((Get-Content .env | Where-Object { $_ -match '^PYTHON_EXECUTABLE=' } | Select-Object -First 1) -replace '^PYTHON_EXECUTABLE=', '')
if (-not $python) { throw 'Set PYTHON_EXECUTABLE in .env to an installed Python environment' }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m ruff check scripts\showcase.py scripts\local_demo.py agents\calibrate_reviews.py src\services\normalize.py tests\test_checks.py tests\test_showcase.py tests\test_local_translation.py tests\test_review_calibration.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git --no-pager diff --check
exit $LASTEXITCODE
