# Azure resources and connection TODOs

These are **user/tenant-admin actions**, not completed deployment claims. No Azure resources
were created from this GitHub session. Work in order; the source implementation covers phases
1a through 6, while these checks establish live acceptance.

## AZURE-CONNECT-1 — Subscription and identity

- [ ] Choose subscription, deployment region, spending limit and model quota.
- [ ] Install Azure CLI, azd, Functions Core Tools v4, Python 3.11; run `az login` and `azd auth login`.
- [ ] Confirm your deployment identity can create resources and RBAC role assignments
  (Contributor plus Role Based Access Control Administrator, or equivalent approved permissions).
- [ ] Register providers: Microsoft.Web, Microsoft.Storage, Microsoft.CognitiveServices,
  Microsoft.DurableTask, Microsoft.ManagedIdentity, Microsoft.Insights and Microsoft.OperationalInsights.
- [ ] For the renderer, also register Microsoft.App and Microsoft.ContainerRegistry.

## AZURE-CONNECT-2 — API app registration and Copilot Studio connection

- [ ] Create a single-tenant Entra app registration; set API requested access-token version to **2**.
- [ ] Set application ID URI to `api://<API_CLIENT_ID>`.
- [ ] Expose delegated scope `Translation.Invoke` with user/admin consent per tenant policy.
- [ ] For app-only flows, expose application role `Translation.Invoke.All` (allowed member type
  Applications) and have an administrator assign it to the flow/service principal.
- [ ] Configure the Copilot Studio/Power Automate custom connector's OAuth application,
  callback URL and delegated permission `api://<API_CLIENT_ID>/Translation.Invoke`.
  Use the connector's secure credential store; never commit secrets to this repository.
- [ ] Set `azd env set API_CLIENT_ID "<client-id>"` and
  `azd env set API_ALLOWED_AUDIENCE "api://<client-id>"`; confirm Easy Auth accepts the v2 issuer
  for your tenant and the API audience. Anonymous deployed calls must return 401.

## AZURE-CONNECT-3 — Provision app resources (Bicep creates these)

- [ ] Initialize the azd environment and set `AZURE_LOCATION`, `FOUNDRY_BASE_MODEL_NAME`,
  `FOUNDRY_MODEL_VERSION`, `FOUNDRY_MODEL_SKU` and `FOUNDRY_MODEL_CAPACITY`.
  `FOUNDRY_MODEL_NAME` is a deployment-name output, not the base model input.
- [ ] Run `azd up`. Bicep creates the Function Flex plan/app, Durable Task Scheduler/hub, storage,
  Table `jobs`, Translator with system MI, Document Intelligence, Foundry account/project/model,
  Function user-assigned MI, Log Analytics, Application Insights, lifecycle policy and RBAC.
- [ ] Check `azd env get-values` and copy the non-secret endpoints/settings to your local environment.
- [ ] Verify Translator MI → Storage Blob Data Contributor, Function MI → Blob/Table roles,
  Cognitive Services User on Translator and Document Intelligence, and Azure AI User on Foundry.
- [ ] Allow time for RBAC propagation, then verify the local developer has equivalent data-plane
  access to Storage, Translator, Document Intelligence and the Foundry project plus Durable Task
  Data Contributor. Never paste keys, SAS tokens or access tokens into chat or source control.

## AZURE-CONNECT-4 — Models and agent versions

- [ ] Confirm the chosen deployment supports structured outputs and Foundry prompt agents.
- [ ] Export `FOUNDRY_PROJECT_ENDPOINT` and the actual deployment name `FOUNDRY_MODEL_NAME`.
- [ ] Run `PYTHONPATH=src python agents/create_agents.py --write-env .agent-versions.env`.
- [ ] Persist the returned `TERM_GUARD_VERSION` and `CHECKER_VERSION` with `azd env set`;
  rerun `azd provision` and `azd deploy` to apply them. A second identical agent-creation run
  must reuse versions, not create duplicates.

## AZURE-CONNECT-5 — Image-slide rendering and SharePoint

- [ ] Create a separate single-tenant Entra renderer application, requested token version 2.
  Expose application role `Renderer.Invoke`; grant it to the Function managed identity
  (and the local developer's test service principal if needed).
- [ ] Deploy `renderer/` with its identity-based ACR/Container Apps script, or provide an existing
  equivalent renderer. Set `RENDERER_CLIENT_ID`, tenant, resource group, location and globally unique
  `RENDERER_REGISTRY`. Restrict ingress/egress according to customer policy.
- [ ] Set `SLIDE_RENDER_URL` and `SLIDE_RENDER_SCOPE=api://<renderer-client-id>/.default`
  in azd and reprovision. Renderer must reject tokens without `Renderer.Invoke`.
- [ ] Set exact `SHAREPOINT_HOSTS`, including the tenant's OneDrive host if used.
- [ ] Choose Graph **delegated** (`Files.ReadWrite`, `X-Graph-Token`) or **application**
  (`Files.ReadWrite.All` on Function MI) access to the sharing-link API. Have the tenant admin
  approve the broad sharing-link permissions or request a narrower drive/item integration.
  API and Graph tokens are distinct; never reuse one for the other.

## AZURE-CONNECT-6 — Live spike, acceptance and notifications

- [ ] Generate samples and run `PYTHONPATH=src:. python scripts/translator_spike.py`.
- [ ] Confirm the actual documented **Document Intelligence PDF mode** for this resource/API.
  The published OpenAPI has no such named mode option; do not reintroduce the invented flags.
  Verify both native/table and image-only PDF outputs before accepting phase 1a.
- [ ] Verify screenshot-only `SIE-2025-001207` survives PPTX translation and is included in OCR checks.
- [ ] Submit multipart and base64 jobs; check 202, owner-authorized polling, report and result links.
  Test encrypted PDFs, `.ppt`, oversize documents, invalid tokens and cross-user access failures.
- [ ] Run an English document to Italian and Spanish; confirm per-direction glossary and locales.
- [ ] Wire the Copilot Studio flow: get file → POST → bounded delay/poll → authenticated result/report fetch.
  Return `jobId`/processing before the interactive response deadline for long jobs and add a separate
  status action or background flow. For a clickable link, save the result with the SharePoint/OneDrive
  connector and return its permission-aware web URL plus score/top reasons. Use secure inputs/outputs.
- [ ] Add customer outputs to ignored `eval/baseline/`; run `PYTHONPATH=src:. python eval/run_eval.py`.
  Inspect the CSV and agree pass thresholds/weights with the customer.
- [ ] Exercise a flagged document; check every PDF header/PPTX footer and report contents.
- [ ] Approve and implement Teams/email delivery in `src/activities/notify.py`; it is a stub,
  not a completed integration.
- [ ] Verify Application Insights jobId correlation and usage counters without document/token logs.
- [ ] Verify lifecycle and daily dynamic-container cleanup after the retention period. Approve a
  separate policy for Table summaries, Durable history and Foundry service-side retention.
