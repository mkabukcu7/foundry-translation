#!/usr/bin/env bash
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

set -euo pipefail

required=(
  DTS_ENDPOINT TASKHUB_NAME TRANSLATOR_ENDPOINT DOCINTEL_ENDPOINT
  FOUNDRY_PROJECT_ENDPOINT FOUNDRY_MODEL_NAME STORAGE_BLOB_ENDPOINT
  STORAGE_QUEUE_ENDPOINT STORAGE_TABLE_ENDPOINT API_CLIENT_ID AZURE_TENANT_ID
)
for name in "${required[@]}"; do
  if [[ -z "$(printenv "$name" || true)" ]]; then
    printf 'Error: %s is not set. Run azd provision and configure the required environment values.\n' "$name" >&2
    exit 1
  fi
done

python3 - <<'PY'
import json
import os
from pathlib import Path

names = (
    "DTS_ENDPOINT",
    "TASKHUB_NAME",
    "TRANSLATOR_ENDPOINT",
    "DOCINTEL_ENDPOINT",
    "FOUNDRY_PROJECT_ENDPOINT",
    "FOUNDRY_MODEL_NAME",
    "TERM_GUARD_MODEL_NAME",
    "CHECKER_MODEL_NAME",
    "MISTRAL_CHECKER_MODEL_NAME",
    "FOUNDRY_MODEL_ENDPOINT",
    "STORAGE_BLOB_ENDPOINT",
    "STORAGE_QUEUE_ENDPOINT",
    "STORAGE_TABLE_ENDPOINT",
    "AZURE_LOCATION",
    "AZURE_RESOURCE_GROUP",
    "AZURE_FUNCTION_NAME",
    "API_BASE_URL",
    "API_CLIENT_ID",
    "AZURE_TENANT_ID",
    "API_IDENTITY_CLIENT_ID",
    "API_IDENTITY_PRINCIPAL_ID",
    "API_ALLOWED_AUDIENCE",
    "MAX_BASE64_MB",
    "MAX_MULTIPART_MB",
    "PASS_THRESHOLD",
    "ID_REGEXES_JSON",
    "TERM_GUARD_VERSION",
    "CHECKER_VERSION",
    "SHAREPOINT_HOSTS",
    "SLIDE_RENDER_URL",
    "SLIDE_RENDER_SCOPE",
    "SOURCE_LOCALE",
    "TARGET_LOCALE",
)
defaults = {
    "API_ALLOWED_AUDIENCE": os.environ["API_CLIENT_ID"],
    "MAX_BASE64_MB": "10",
    "MAX_MULTIPART_MB": "40",
    "PASS_THRESHOLD": "85",
    "ID_REGEXES_JSON": "[]",
}
values = {name: os.environ.get(name) or defaults.get(name, "") for name in names}
values.update(
    {
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "AzureWebJobsStorage__blobServiceUri": values["STORAGE_BLOB_ENDPOINT"],
        "AzureWebJobsStorage__queueServiceUri": values["STORAGE_QUEUE_ENDPOINT"],
        "AzureWebJobsStorage__tableServiceUri": values["STORAGE_TABLE_ENDPOINT"],
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": (
            f"Endpoint={values['DTS_ENDPOINT']};Authentication=DefaultAzure"
        ),
    }
)
settings = {"IsEncrypted": False, "Values": values}
Path("src/local.settings.json").write_text(
    json.dumps(settings, indent=2) + "\n", encoding="utf-8"
)
print("Generated src/local.settings.json using DefaultAzureCredential-compatible endpoints.")
PY
