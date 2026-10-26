#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Deploy only after choosing the subscription and creating the renderer Entra application.
# Uses an AcrPull managed identity; registry admin credentials remain disabled.
set -euo pipefail
: "${AZURE_RESOURCE_GROUP:?Set AZURE_RESOURCE_GROUP}"
: "${AZURE_LOCATION:?Set AZURE_LOCATION}"
: "${AZURE_TENANT_ID:?Set AZURE_TENANT_ID}"
: "${RENDERER_CLIENT_ID:?Set RENDERER_CLIENT_ID}"
: "${RENDERER_REGISTRY:?Set globally unique alphanumeric RENDERER_REGISTRY}"
name="${RENDERER_APP_NAME:-aon-slide-renderer}"
environment="${RENDERER_ENVIRONMENT:-aon-renderer}"
az acr create -g "$AZURE_RESOURCE_GROUP" -n "$RENDERER_REGISTRY" --sku Basic --admin-enabled false -o none
az acr build -r "$RENDERER_REGISTRY" --image renderer:v1 "$(dirname "$0")" -o none
identity_id=$(az identity create -g "$AZURE_RESOURCE_GROUP" -n "${name}-pull" --query id -o tsv)
principal_id=$(az identity show --ids "$identity_id" --query principalId -o tsv)
registry_id=$(az acr show -n "$RENDERER_REGISTRY" --query id -o tsv)
az role assignment create --assignee-object-id "$principal_id" --assignee-principal-type ServicePrincipal \
  --role AcrPull --scope "$registry_id" -o none
az containerapp env create -g "$AZURE_RESOURCE_GROUP" -n "$environment" -l "$AZURE_LOCATION" -o none
az containerapp create -g "$AZURE_RESOURCE_GROUP" -n "$name" --environment "$environment" \
  --image "${RENDERER_REGISTRY}.azurecr.io/renderer:v1" \
  --registry-server "${RENDERER_REGISTRY}.azurecr.io" --registry-identity "$identity_id" \
  --user-assigned "$identity_id" --ingress external --target-port 8000 --cpu 1 --memory 2Gi \
  --min-replicas 0 --max-replicas 2 \
  --env-vars "AZURE_TENANT_ID=$AZURE_TENANT_ID" "RENDERER_CLIENT_ID=$RENDERER_CLIENT_ID" -o none
host=$(az containerapp show -g "$AZURE_RESOURCE_GROUP" -n "$name" --query properties.configuration.ingress.fqdn -o tsv)
printf 'SLIDE_RENDER_URL=https://%s/render\nSLIDE_RENDER_SCOPE=api://%s/.default\n' "$host" "$RENDERER_CLIENT_ID"
