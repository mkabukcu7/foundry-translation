# SPDX-License-Identifier: MIT
$ErrorActionPreference = 'Stop'
$python = if (Test-Path .env) {
    ((Get-Content .env | Where-Object { $_ -match '^PYTHON_EXECUTABLE=' } | Select-Object -First 1) -replace '^PYTHON_EXECUTABLE=', '')
}
if (-not $python) { throw 'Set PYTHON_EXECUTABLE in .env to an installed Python environment' }
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m ruff check src\services\local_translation.py src\services\docintel_client.py src\activities\prepare.py src\services\foundry_agents.py src\activities\check.py agents scripts\local_demo.py scripts\verify_local_demo.py tests\test_local_translation.py tests\test_foundry_agents.py samples\generate_local_samples.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python -m samples.generate_local_samples
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git --no-pager diff --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git --no-pager status --short
exit $LASTEXITCODE
