# SPDX-License-Identifier: MIT
# Read-only verification of the deployed prompt agents and their underlying models.
param([switch]$SmokeTest)

$ErrorActionPreference = 'Stop'
Get-Content (Join-Path $PSScriptRoot '..\.env') | ForEach-Object {
    if ($_ -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
        Set-Item "env:$($Matches[1])" $Matches[2]
    }
}
if (Test-Path (Join-Path $PSScriptRoot '..\.agent-versions.env')) {
    Get-Content (Join-Path $PSScriptRoot '..\.agent-versions.env') | ForEach-Object {
        if ($_ -match '^((?:TERM_GUARD|CHECKER)_VERSION)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] }
    }
}
$endpoint = $env:FOUNDRY_PROJECT_ENDPOINT.TrimEnd('/')
if ($endpoint -notmatch '^https://([^.]+)\.services\.ai\.azure\.com/api/projects/([^/]+)$') {
    throw 'Expected a Foundry project endpoint in .env'
}
$account = $Matches[1]
az account show --query name -o tsv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

foreach ($entry in @(
    @{Name = 'term-guard'; Version = $env:TERM_GUARD_VERSION},
    @{Name = 'translation-checker'; Version = $env:CHECKER_VERSION}
)) {
    $url = "$endpoint/agents/$($entry.Name)/versions/$($entry.Version)?api-version=2025-11-15-preview"
    $raw = az rest --method get --url $url --resource https://ai.azure.com -o json
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $agent = $raw | ConvertFrom-Json
    [ordered]@{
        agent = $agent.name
        version = $agent.version
        kind = $agent.definition.kind
        modelDeployment = $agent.definition.model
        responseFormat = $agent.definition.text.format.type
        definitionHash = $agent.metadata.definitionHash
    } | ConvertTo-Json
}

$raw = az cognitiveservices account list -o json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$resource = @($raw | ConvertFrom-Json) | Where-Object name -eq $account
if (@($resource).Count -ne 1) { throw 'Could not uniquely resolve the Foundry account in the active subscription' }
$raw = az cognitiveservices account deployment list --name $account --resource-group $resource.resourceGroup -o json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
@($raw | ConvertFrom-Json) | Where-Object {
    $_.name -in @($env:FOUNDRY_MODEL_NAME, $env:TERM_GUARD_MODEL_NAME,
        $env:CHECKER_MODEL_NAME, $env:MISTRAL_CHECKER_MODEL_NAME)
} | ForEach-Object {
    [ordered]@{
        deployment = $_.name
        state = $_.properties.provisioningState
        model = $_.properties.model.name
        modelVersion = $_.properties.model.version
    } | ConvertTo-Json
}

if ($SmokeTest) {
    $env:PYTHONPATH = Join-Path $PSScriptRoot '..\src'
    if (-not $env:PYTHON_EXECUTABLE) {
        throw 'Set PYTHON_EXECUTABLE in .env to run the synthetic smoke test'
    }
    & $env:PYTHON_EXECUTABLE (Join-Path $PSScriptRoot 'smoke_test.py')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
