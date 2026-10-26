// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

param location string
param tags object
param translatorName string
param documentIntelligenceName string
param foundryName string
param foundryProjectName string
param foundryDeploymentName string
param foundryModelName string
param foundryModelVersion string
param foundryModelSku string
param foundryModelCapacity int

resource translator 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: translatorName
  location: location
  kind: 'TextTranslation'
  sku: {
    name: 'S1'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: translatorName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: documentIntelligenceName
  location: location
  kind: 'FormRecognizer'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: documentIntelligenceName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: foundryName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
  tags: tags
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: foundryProjectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Foundry Translation'
    description: 'Project used by the document translation service.'
  }
  tags: tags
}

resource foundryDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: foundryDeploymentName
  sku: {
    name: foundryModelSku
    capacity: foundryModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: foundryModelName
      version: foundryModelVersion
    }
  }
}

output translatorEndpoint string = translator.properties.endpoint
output translatorPrincipalId string = translator.identity.principalId
output documentIntelligenceEndpoint string = documentIntelligence.properties.endpoint
output foundryProjectEndpoint string = 'https://${foundryName}.services.ai.azure.com/api/projects/${foundryProjectName}'
output deploymentName string = foundryDeploymentName
