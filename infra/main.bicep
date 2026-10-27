// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('azd environment name used to make resource names unique.')
param environmentName string

@minLength(1)
@description('Primary Azure region for the resources.')
@allowed([
  'australiaeast'
  'centralus'
  'eastasia'
  'eastus2'
  'northeurope'
  'southeastasia'
  'swedencentral'
  'uksouth'
  'westus2'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

@description('Entra application registration client ID for the protected HTTP API. Create the app registration separately.')
@minLength(1)
param apiClientId string

@description('Allowed API audience value registered for the HTTP API.')
@minLength(1)
param apiAllowedAudience string = apiClientId

@description('Foundry model identifier.')
param foundryModelName string = 'gpt-4o-mini'

@description('Foundry model version.')
param foundryModelVersion string = '2024-07-18'

@description('Foundry model deployment SKU.')
param foundryModelSku string = 'Standard'

@minValue(1)
@description('Foundry model deployment capacity.')
param foundryModelCapacity int = 1

@description('Optional JSON array of ID regular expressions.')
param idRegexes array = []

@description('Optional source locale override; blank derives it from source language.')
param sourceLocale string = ''

@description('Optional target locale override; blank derives it from target language.')
param targetLocale string = ''

@minValue(1)
param maxBase64Mb int = 10

@minValue(1)
param maxMultipartMb int = 40

@minValue(0)
@maxValue(100)
param passThreshold int = 85

@description('Optional TERM_GUARD_VERSION used by the processing pipeline.')
param termGuardVersion string = ''

@description('Optional CHECKER_VERSION used by the processing pipeline.')
param checkerVersion string = ''

@description('Optional existing deployment override for Term Guard.')
param termGuardModelName string = ''

@description('Optional existing deployment override for the primary checker.')
param checkerModelName string = ''

@description('Optional existing Mistral deployment; blank disables the second checker.')
param mistralCheckerModelName string = ''

@description('OpenAI v1 endpoint for the optional second checker.')
param foundryModelEndpoint string = ''

@description('Optional comma-separated SharePoint host allowlist.')
param sharePointHosts string = ''

@description('Optional external Microsoft Entra-authenticated slide-render endpoint. Flex Consumption has no local LibreOffice dependency.')
param slideRenderUrl string = ''

@description('Optional Microsoft Entra scope for the slide-render endpoint.')
param slideRenderScope string = ''

@description('Durable Task Scheduler resource SKU.')
param dtsSkuName string = 'Consumption'

var token = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var resourceGroupName = 'rg-${environmentName}'
var functionAppName = 'func-${token}'
var identityName = 'id-${token}'
var planName = 'plan-${token}'
var storageAccountName = 'st${token}'
var translatorName = 'translator-${token}'
var documentIntelligenceName = 'docintel-${token}'
var foundryName = 'foundry-${token}'
var foundryProjectName = 'translation-${take(token, 12)}'
var foundryDeploymentName = 'translation-model'
var deploymentStorageContainerName = 'function-packages'
var dtsName = 'dts-${token}'
var taskHubName = 'th-${token}'
var logAnalyticsName = 'log-${token}'
var applicationInsightsName = 'appi-${token}'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module apiIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'functionIdentity'
  scope: resourceGroup
  params: {
    name: identityName
    location: location
    tags: tags
  }
}

module appServicePlan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: 'flexConsumptionPlan'
  scope: resourceGroup
  params: {
    name: planName
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
    reserved: true
    location: location
    tags: tags
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'storage'
  scope: resourceGroup
  params: {
    name: storageAccountName
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [
        { name: deploymentStorageContainerName }
        { name: 'source' }
        { name: 'reports' }
        { name: 'glossaries' }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

module storageConfiguration './app/storage-configuration.bicep' = {
  name: 'storageConfiguration'
  scope: resourceGroup
  params: {
    storageAccountName: storage.outputs.name
  }
}

module aiServices './app/ai-services.bicep' = {
  name: 'aiServices'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    translatorName: translatorName
    documentIntelligenceName: documentIntelligenceName
    foundryName: foundryName
    foundryProjectName: foundryProjectName
    foundryDeploymentName: foundryDeploymentName
    foundryModelName: foundryModelName
    foundryModelVersion: foundryModelVersion
    foundryModelSku: foundryModelSku
    foundryModelCapacity: foundryModelCapacity
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.11.1' = {
  name: 'logAnalytics'
  scope: resourceGroup
  params: {
    name: logAnalyticsName
    location: location
    tags: tags
    dataRetention: 30
  }
}

module monitoring 'br/public:avm/res/insights/component:0.6.0' = {
  name: 'applicationInsights'
  scope: resourceGroup
  params: {
    name: applicationInsightsName
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
    disableLocalAuth: true
  }
}

module dts './app/dts.bicep' = {
  name: 'durableTaskScheduler'
  scope: resourceGroup
  params: {
    name: dtsName
    taskhubname: taskHubName
    location: location
    tags: tags
    ipAllowlist: [
      '0.0.0.0/0'
    ]
    skuName: dtsSkuName
  }
}

module api './app/api.bicep' = {
  name: 'functionApp'
  scope: resourceGroup
  params: {
    name: functionAppName
    location: location
    tags: tags
    applicationInsightsName: monitoring.outputs.name
    appServicePlanId: appServicePlan.outputs.resourceId
    runtimeName: 'python'
    runtimeVersion: '3.11'
    storageAccountName: storage.outputs.name
    deploymentStorageContainerName: deploymentStorageContainerName
    identityId: apiIdentity.outputs.resourceId
    identityClientId: apiIdentity.outputs.clientId
    dtsURL: dts.outputs.dts_URL
    taskHubName: dts.outputs.TASKHUB_NAME
    appSettings: {
      MAX_BASE64_MB: string(maxBase64Mb)
      MAX_MULTIPART_MB: string(maxMultipartMb)
      PASS_THRESHOLD: string(passThreshold)
      SOURCE_LOCALE: sourceLocale
      TARGET_LOCALE: targetLocale
      ID_REGEXES_JSON: string(idRegexes)
      TERM_GUARD_VERSION: termGuardVersion
      CHECKER_VERSION: checkerVersion
      TERM_GUARD_MODEL_NAME: termGuardModelName
      CHECKER_MODEL_NAME: checkerModelName
      MISTRAL_CHECKER_MODEL_NAME: mistralCheckerModelName
      FOUNDRY_MODEL_ENDPOINT: foundryModelEndpoint
      SHAREPOINT_HOSTS: sharePointHosts
      SLIDE_RENDER_URL: slideRenderUrl
      SLIDE_RENDER_SCOPE: slideRenderScope
      TRANSLATOR_ENDPOINT: aiServices.outputs.translatorEndpoint
      DOCINTEL_ENDPOINT: aiServices.outputs.documentIntelligenceEndpoint
      FOUNDRY_PROJECT_ENDPOINT: aiServices.outputs.foundryProjectEndpoint
      FOUNDRY_MODEL_NAME: aiServices.outputs.deploymentName
      STORAGE_BLOB_ENDPOINT: storage.outputs.primaryBlobEndpoint
      STORAGE_QUEUE_ENDPOINT: replace(storage.outputs.primaryBlobEndpoint, '.blob.', '.queue.')
      STORAGE_TABLE_ENDPOINT: replace(storage.outputs.primaryBlobEndpoint, '.blob.', '.table.')
      AZURE_RESOURCE_GROUP: resourceGroupName
      API_BASE_URL: 'https://${functionAppName}.azurewebsites.net/api'
      API_CLIENT_ID: apiClientId
      API_ALLOWED_AUDIENCE: apiAllowedAudience
      AZURE_CLIENT_ID: apiIdentity.outputs.clientId
      AZURE_TENANT_ID: tenant().tenantId
      DTS_ENDPOINT: dts.outputs.dts_URL
      TASKHUB_NAME: dts.outputs.TASKHUB_NAME
    }
  }
}

module apiAuth './app/auth-settings.bicep' = {
  name: 'apiAuthentication'
  scope: resourceGroup
  params: {
    functionAppName: functionAppName
    tenantId: tenant().tenantId
    apiClientId: apiClientId
    apiAllowedAudience: apiAllowedAudience
  }
  dependsOn: [
    api
  ]
}

module rbac './app/rbac.bicep' = {
  name: 'roleAssignments'
  scope: resourceGroup
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiIdentity.outputs.principalId
    translatorPrincipalId: aiServices.outputs.translatorPrincipalId
    translatorResourceName: translatorName
    documentIntelligenceResourceName: documentIntelligenceName
    foundryResourceName: foundryName
    foundryProjectName: foundryProjectName
    dtsName: dtsName
  }
  dependsOn: [
    dts
  ]
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_TENANT_ID string = tenant().tenantId
output API_CLIENT_ID string = apiClientId
output API_ALLOWED_AUDIENCE string = apiAllowedAudience
output API_IDENTITY_CLIENT_ID string = apiIdentity.outputs.clientId
output API_IDENTITY_PRINCIPAL_ID string = apiIdentity.outputs.principalId
output TERM_GUARD_VERSION string = termGuardVersion
output CHECKER_VERSION string = checkerVersion
output TERM_GUARD_MODEL_NAME string = termGuardModelName
output CHECKER_MODEL_NAME string = checkerModelName
output MISTRAL_CHECKER_MODEL_NAME string = mistralCheckerModelName
output FOUNDRY_MODEL_ENDPOINT string = foundryModelEndpoint
output SLIDE_RENDER_URL string = slideRenderUrl
output SLIDE_RENDER_SCOPE string = slideRenderScope
output SHAREPOINT_HOSTS string = sharePointHosts
output ID_REGEXES_JSON string = string(idRegexes)
output SOURCE_LOCALE string = sourceLocale
output TARGET_LOCALE string = targetLocale
output MAX_BASE64_MB string = string(maxBase64Mb)
output MAX_MULTIPART_MB string = string(maxMultipartMb)
output PASS_THRESHOLD string = string(passThreshold)
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
output API_BASE_URL string = 'https://${functionAppName}.azurewebsites.net/api'
output TRANSLATOR_ENDPOINT string = aiServices.outputs.translatorEndpoint
output DOCINTEL_ENDPOINT string = aiServices.outputs.documentIntelligenceEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = aiServices.outputs.foundryProjectEndpoint
output FOUNDRY_MODEL_NAME string = aiServices.outputs.deploymentName
output STORAGE_BLOB_ENDPOINT string = storage.outputs.primaryBlobEndpoint
output STORAGE_QUEUE_ENDPOINT string = replace(storage.outputs.primaryBlobEndpoint, '.blob.', '.queue.')
output STORAGE_TABLE_ENDPOINT string = replace(storage.outputs.primaryBlobEndpoint, '.blob.', '.table.')
output DTS_ENDPOINT string = dts.outputs.dts_URL
output TASKHUB_NAME string = dts.outputs.TASKHUB_NAME
