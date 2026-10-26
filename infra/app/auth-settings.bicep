// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

param functionAppName string
param tenantId string
param apiClientId string
param apiAllowedAudience string

resource functionApp 'Microsoft.Web/sites@2022-09-01' existing = {
  name: functionAppName
}

resource apiAuth 'Microsoft.Web/sites/config@2022-09-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: apiClientId
        }
        validation: {
          allowedAudiences: [
            apiAllowedAudience
          ]
        }
      }
    }
  }
}
