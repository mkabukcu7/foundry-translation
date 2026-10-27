# SPDX-License-Identifier: MIT
param(
    [Parameter(Mandatory)][string]$Subscription,
    [Parameter(Mandatory)][string]$ResourceGroup,
    [string]$Location = 'eastus2'
)
$ErrorActionPreference = 'Stop'
$envPath = Join-Path $PSScriptRoot '..\.env'
$existing = Get-Content $envPath
if ($existing -match '^TRANSLATOR_ENDPOINT=' -or $existing -match '^DOCINTEL_ENDPOINT=') {
    throw 'Service endpoints already configured; inspect existing resources rather than provisioning duplicates.'
}
$principal = az ad signed-in-user show --query id -o tsv
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 10)
$settings = @()
foreach ($service in @(
    @{Name = "translator-demo-$suffix"; Kind = 'TextTranslation'; Sku = 'S1'; Setting = 'TRANSLATOR_ENDPOINT'},
    @{Name = "docintel-demo-$suffix"; Kind = 'FormRecognizer'; Sku = 'S0'; Setting = 'DOCINTEL_ENDPOINT'}
)) {
    $id = "/subscriptions/$Subscription/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/$($service.Name)"
    $body = @{
        location = $Location
        kind = $service.Kind
        sku = @{name = $service.Sku}
        properties = @{customSubDomainName = $service.Name; disableLocalAuth = $true; publicNetworkAccess = 'Enabled'}
        tags = @{purpose = 'local-translation-demo'}
    } | ConvertTo-Json -Depth 5
    $temp = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content $temp $body -Encoding utf8
        az rest --method put --url "https://management.azure.com${id}?api-version=2025-06-01" --body "@$temp" --output none
        if ($LASTEXITCODE -ne 0) { throw "Resource creation failed: $($service.Kind)" }
    } finally {
        Remove-Item -LiteralPath $temp
    }
    az role assignment create --assignee-object-id $principal --assignee-principal-type User `
        --role 'Cognitive Services User' --scope $id --subscription $Subscription --output none
    if ($LASTEXITCODE -ne 0) { throw "Role assignment failed: $($service.Kind); resource already exists: $($service.Name)" }
    $endpoint = "https://$($service.Name).cognitiveservices.azure.com"
    "$($service.Setting)=$endpoint" | Add-Content $envPath
    $settings += @{name = $service.Name; kind = $service.Kind; endpoint = $endpoint}
}
$settings | ConvertTo-Json
