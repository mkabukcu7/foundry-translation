# Infrastructure integration contract

Copyright (c) Microsoft Corporation. Licensed under the MIT License.

The infrastructure foundation is adapted from
[Azure-Samples/durable-functions-quickstart-python-azd](https://github.com/Azure-Samples/durable-functions-quickstart-python-azd):
subscription-scoped azd deployment, pinned Azure Verified Modules, Python Flex
Consumption hosting, managed-identity storage and telemetry, and Durable Task
Scheduler. Upstream files were retrieved from `main`; an upstream commit SHA was
not recorded. See the repository `LICENSE.md`.

## Inputs

`infra/main.parameters.json` binds these azd environment variables. Use
`azd env set NAME VALUE`; no application `.env` file is sourced by the hooks.

| azd variable | Bicep parameter | Default / requirement |
| --- | --- | --- |
| `AZURE_ENV_NAME` | `environmentName` | Required, normally selected by azd |
| `AZURE_LOCATION` | `location` | Required, region supported by Flex and selected services |
| `API_CLIENT_ID` | `apiClientId` | Required, existing Entra application client ID |
| `API_ALLOWED_AUDIENCE` | `apiAllowedAudience` | Optional; defaults to the client ID |
| `FOUNDRY_BASE_MODEL_NAME` | `foundryModelName` | `gpt-4o-mini` |
| `FOUNDRY_MODEL_VERSION` | `foundryModelVersion` | `2024-07-18` |
| `FOUNDRY_MODEL_SKU` | `foundryModelSku` | `Standard` |
| `FOUNDRY_MODEL_CAPACITY` | `foundryModelCapacity` | `1` |
| `TERM_GUARD_VERSION` | `termGuardVersion` | Empty, allowed during initial provisioning |
| `CHECKER_VERSION` | `checkerVersion` | Empty, allowed during initial provisioning |
| `TERM_GUARD_MODEL_NAME` | `termGuardModelName` | Empty, falls back to the provisioned deployment |
| `CHECKER_MODEL_NAME` | `checkerModelName` | Empty, falls back to the provisioned deployment |
| `MISTRAL_CHECKER_MODEL_NAME` | `mistralCheckerModelName` | Empty, disables independent review |
| `FOUNDRY_MODEL_ENDPOINT` | `foundryModelEndpoint` | Empty, required with independent review |
| `SLIDE_RENDER_URL` | `slideRenderUrl` | Empty, external render service optional |
| `SLIDE_RENDER_SCOPE` | `slideRenderScope` | Empty, configure with the external render service |
| `SHAREPOINT_HOSTS` | `sharePointHosts` | Empty, comma-separated allowed hosts |
| `ID_REGEXES_JSON` | `idRegexes` | `[]`, JSON array |
| `SOURCE_LOCALE` | `sourceLocale` | Empty, derive from detected/configured language |
| `TARGET_LOCALE` | `targetLocale` | Empty, derive from configured language |
| `MAX_BASE64_MB` | `maxBase64Mb` | `10` |
| `MAX_MULTIPART_MB` | `maxMultipartMb` | `40` |
| `PASS_THRESHOLD` | `passThreshold` | `85` |
| `DTS_SKU_NAME` | `dtsSkuName` | `Consumption` |

The model input is deliberately **`FOUNDRY_BASE_MODEL_NAME`**, not
`FOUNDRY_MODEL_NAME`. The latter is an output/runtime setting containing the
deployment name, `translation-model`, which the SDK uses for inference.
Select a model/version/SKU/capacity combination available in the chosen region
and subscription quota.

Role overrides reference existing deployments; this template does not create those
additional deployments. For Mistral, use the account's HTTPS `/openai/v1/` endpoint.
When using deployments in another account, grant the runtime identity inference
access on that account separately. These values also flow to local settings.

Initial `azd up` is allowed with both agent-version values empty. After creating
the project/model, run the agent creation helper, set its returned version values
with `azd env set TERM_GUARD_VERSION ...` and `azd env set CHECKER_VERSION ...`,
then run `azd provision` again. This updates function settings and regenerates
local settings through the postprovision hook. No Entra application is created
by this deployment.

## Outputs

- `AZURE_LOCATION`, `AZURE_RESOURCE_GROUP`, `AZURE_TENANT_ID`
- `API_CLIENT_ID`, `API_ALLOWED_AUDIENCE`
- `API_IDENTITY_CLIENT_ID`, `API_IDENTITY_PRINCIPAL_ID`
- `AZURE_FUNCTION_NAME`, `API_BASE_URL` (`https://<function>.azurewebsites.net/api`)
- `TRANSLATOR_ENDPOINT`, `DOCINTEL_ENDPOINT`
- `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL_NAME` (deployment name)
- `STORAGE_BLOB_ENDPOINT`, `STORAGE_QUEUE_ENDPOINT`, `STORAGE_TABLE_ENDPOINT`
- `DTS_ENDPOINT`, `TASKHUB_NAME`
- `TERM_GUARD_VERSION`, `CHECKER_VERSION`
- `SLIDE_RENDER_URL`, `SLIDE_RENDER_SCOPE`, `SHAREPOINT_HOSTS`
- `ID_REGEXES_JSON`, `SOURCE_LOCALE`, `TARGET_LOCALE`
- `MAX_BASE64_MB`, `MAX_MULTIPART_MB`, `PASS_THRESHOLD`

Use `API_IDENTITY_PRINCIPAL_ID` when assigning the Function's user-assigned managed
identity a role on an external service; its client ID is `API_IDENTITY_CLIENT_ID`.
The deployment sets `AZURE_CLIENT_ID` on the Function for SDK managed-identity
selection, but intentionally does not put it in local settings.

## Local settings and retention

Both postprovision helpers regenerate `src/local.settings.json` from environment
values, including pinned agent versions, optional configuration, and blank
locale overrides. Function storage uses deployed blob/queue/table service URIs
with the local credential chain; managed-identity credential/client-ID settings
are not copied locally. DTS uses `Authentication=DefaultAzure`; no emulator,
shared key, SAS, or credential secret is written.

Static lifecycle rules remove source, reports, and per-job glossary blobs after
14 days. There is no wildcard/container-prefix lifecycle rule for dynamically
named `target-{jobId}` containers. Application timer cleanup handles those
containers and job metadata. Deployment packages are excluded from retention.

## Offline validation

Standalone Bicep `0.38.33` (`6bb5d5f859`) successfully compiled `infra/main.bicep`.
The compiler emits three `BCP081` warnings because this compiler's type catalog
does not contain the upstream `Microsoft.DurableTask/schedulers` and
`schedulers/taskHubs` API version `2026-02-01` (including an existing scheduler
reference in RBAC). That API version is retained from the authentic upstream
sample rather than guessed or downgraded.

No live Azure deployment or authorization test was performed. PowerShell
execution requires validation on a machine with `pwsh`; only the POSIX helper
was executable in the development environment.
