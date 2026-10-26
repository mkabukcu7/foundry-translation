# Aon Document Translation POC

Durable Functions workflow: **submit → precheck → prepare → translate → check → route → notify**.
PDF/PPTX input, default source auto-detection and English output, with reverse translation to
Italian or Spanish. Copilot Studio calls an Entra-protected HTTP API.

**Implementation is not evidence of deployment.** Local checks use synthetic files and mocked
Azure boundaries. No Azure subscription is connected in the development session. Complete
[AZURE-TODO.md](AZURE-TODO.md) before claiming live acceptance. In particular, the Translator
PDF/Document Intelligence mode needs confirmation against the actual service and Learn documentation.

## Foundation and layout

The original repository contained only instructions, not the requested quickstart code.
The infrastructure/host foundation is restored from
[Azure-Samples/durable-functions-quickstart-python-azd](https://github.com/Azure-Samples/durable-functions-quickstart-python-azd)
and extended, retaining Python, Flex Consumption, Durable Task Scheduler, identity-based storage,
Application Insights and azd. See `LICENSE.md`.

| Path | Purpose |
|---|---|
| `infra/`, `azure.yaml` | Subscription/resource deployment, identities, RBAC, Entra API authentication |
| `src/function_app.py`, `src/orchestrator.py` | Intake, owner-authorized status/report/download, durable timers |
| `src/activities/`, `src/services/` | Extraction, glossary, translation, deterministic/agent review and routing |
| `src/glossaries/aon-standard.tsv` | 22 approved Italian/Spanish/English insurance terms |
| `src/prompts/`, `src/schemas/`, `agents/` | Structured Foundry prompt agents and idempotent version creation |
| `renderer/` | Entra-protected LibreOffice image-slide renderer for Flex Consumption |
| `samples/`, `scripts/translator_spike.py` | Synthetic documents, seeded oracle, live Translator spike |
| `eval/` | Three-arm live evaluation and optional customer baseline |
| `tests/` | Offline contract, intake, normalization, orchestration and routing tests |

## Setup and deploy

Prerequisites: Python 3.11, Azure CLI, Azure Developer CLI (`azd`), Functions Core Tools v4,
subscription RBAC permissions to create resources and role assignments, and model quota in the
selected region. Local PPTX rendering needs LibreOffice and a font supporting Italian/Spanish.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
az login
azd auth login
az account set --subscription "<subscription-id>"
azd init
azd env set AZURE_LOCATION "<supported-region>"
# Create the Entra API application first; follow AZURE-TODO.md.
azd env set API_CLIENT_ID "<api-application-client-id>"
azd env set API_ALLOWED_AUDIENCE "api://<api-application-client-id>"
azd env set FOUNDRY_BASE_MODEL_NAME "<region-supported-model>"
azd env set FOUNDRY_MODEL_VERSION "<supported-model-version>"
azd env set FOUNDRY_MODEL_SKU "<supported-deployment-sku>"
azd env set FOUNDRY_MODEL_CAPACITY "<approved-capacity>"
azd up
```

Choose a Foundry model/version supported by your subscription/region and configure the model
parameters in `infra/main.parameters.json`/azd environment before provisioning. No Azure keys,
storage shared keys, SAS tokens, or client secrets are used. The Durable Scheduler setting named
`DURABLE_TASK_SCHEDULER_CONNECTION_STRING` is an **identity descriptor**, not a credential.
Application Insights' connection string similarly identifies the telemetry endpoint; ingestion uses MI.
`FOUNDRY_MODEL_NAME` is the resulting **deployment** name, not the base model name above.
The exact parameter/output contract is documented in `infra/README.md`.

After provisioning, load the non-secret outputs/configuration into the shell or a local `.env`
copied from `.env.sample`. For example, export each matching `azd env get-values` output using your
shell's environment-file support. `.env` is not automatically read by these Python scripts.
Do not set the deployed `AZURE_CLIENT_ID` locally: local clients should use your `az login`.
Create agent versions and apply their pinned versions to the Function settings:

```bash
PYTHONPATH=src python agents/create_agents.py --write-env .agent-versions.env
# Use the version numbers printed by the command, then persist settings via azd:
azd env set TERM_GUARD_VERSION "<term-guard-version>"
azd env set CHECKER_VERSION "<translation-checker-version>"
azd provision
azd deploy
```

Deploy `renderer/` using `bash renderer/deploy.sh` after its Entra registration is ready, or use
an existing renderer implementing the same contract. Set `SLIDE_RENDER_URL` and
`SLIDE_RENDER_SCOPE` in azd and reprovision. The renderer accepts raw PPTX at `/render`, requires
an Entra token with application role `Renderer.Invoke`, and returns `application/pdf`.
It does not use anonymous conversion endpoints or forward customer documents to third parties.

## Local run and validation

`scripts/postprovision.*` generate `src/local.settings.json` from deployed resource outputs.
Use your Azure CLI identity and grant the developer the data-plane roles in the checklist.
No shared-key storage emulator is required.

```bash
python samples/generate_samples.py
pytest -q
ruff check .
cd src
func start
```

The generator creates an Italian claim-form PDF with tables, a scanned-style Spanish PDF with
**no text layer**, and a Spanish PPTX containing a screenshot. The claim number `SIE-2025-001207`
exists only inside the screenshot, making image OCR necessary for the seeded oracle.
Generated documents/results are ignored; `samples/expected.json` is committed. Names/identifiers
are fictitious. The packaged MIT `cl100k_base` vocabulary is checksum-verified and avoids a
network download on cold start; its license is in `src/tokenizer/LICENSE`.

For local-only HTTP development, explicitly set `ALLOW_LOCAL_AUTH_BYPASS=1` and
`AZURE_FUNCTIONS_ENVIRONMENT=Development` in local settings. This bypass is rejected when the
Azure host's `WEBSITE_INSTANCE_ID` exists. Never expose the local Functions host publicly or
configure the bypass in deployed settings. Production authentication relies on Azure Easy Auth,
which validates tokens and supplies trusted principal headers.

## HTTP contract

All routes are under `/api`. Use a v2 Entra access token whose audience is the API client ID
(or its configured application ID URI), with delegated scope
**`api://<API_CLIENT_ID>/Translation.Invoke`** or application role **`Translation.Invoke.All`**.
The API app registration must request v2 access tokens. Keep the same authenticated connection
for submit/poll/report/download: ownership is the caller's Entra object ID, never the submitted
`userId`. For app-only connections the service principal owns its jobs; `userId` is attribution,
not an authorization boundary.

```bash
# TOKEN is an API access token obtained by your Entra-enabled connector, not a function key.
curl --fail-with-body "$API_BASE_URL/jobs" -H "Authorization: Bearer $TOKEN" \
  -F "file=@samples/out/claim_form_it.pdf" -F targetLanguage=en
```

JSON alternative (maximum **10 MiB decoded**; multipart maximum **40 MiB**):

```json
{
  "fileName": "claim.pdf",
  "contentBase64": "<raw-base64-no-data-url-prefix>",
  "targetLanguage": "en",
  "sourceLanguage": "it",
  "userId": "<optional-attribution>"
}
```

`sourceLanguage` is optional; omit it for detection. `targetLanguage` defaults to `en`;
`en → it/es` uses `sourceLanguage=en` and the requested target. Other supported source languages
can translate to English, but approved glossary coverage is Italian/Spanish/English only and the
report explicitly records when that coverage is absent. Unsupported service language codes
surface as failed jobs. `.ppt` returns 415 with a conversion message. Size limits are configurable.

Submit returns **202** `{ "jobId": "...", "statusUrl": ".../jobs/<id>" }`.
Poll with `GET /jobs/{id}`; terminal states are `complete`, `needs_review`, and `failed`.
On completion, the response includes score, decision, top five reasons, protected `reportUrl`
and `resultUrl`. `GET /jobs/{id}/report` returns JSON; `GET /jobs/{id}/result` streams the document.
An unfinished report/result returns 409, another caller's job returns 404. These links still require
the API token; they are not public blob or SAS URLs.

### Copilot Studio agent-flow wiring

Create a custom connector for this API using OAuth 2.0 / Microsoft Entra v2, tenant-specific
authorize/token URLs, the API audience and scope `api://<API_CLIENT_ID>/Translation.Invoke`.
Register its exact generated redirect URL in the connector client application; grant consent.
Use the same connector connection for all job actions. Store connector credentials only in
Power Platform's secure connection store, never in flow bodies or this repository.

1. In an agent-triggered flow, accept a file and target language. Map the file's **name**
   to `fileName` and its **contentBytes** to `contentBase64` without double-encoding it.
   Use the JSON submit contract above; for files over 10 MiB use multipart or the URL intake.
2. POST `/jobs`, parse the 202 response and retain `jobId`.
3. Delay 5 seconds and GET `/jobs/{jobId}` until a terminal status. Bound the interactive
   loop (for example 60 seconds): Copilot Studio synchronous tool/flow response deadlines are
   shorter than the pipeline's 30-minute translation deadline. If still running, return
   `jobId` and "processing" promptly; expose a separate status-check action or background flow.
4. On `failed`, return the safe `error`. On completion fetch `/report` and `/result` through
   the authenticated connector, returning score, decision and the top five reasons.
5. To give a user a **clickable file link**, create the downloaded binary in an approved
   SharePoint/OneDrive folder using that platform's connector and return its permission-aware
   web URL. Do not expose raw private blob URLs, invent a public link, or put a bearer token in
   a URL. A bare API `resultUrl` is intended for authenticated actions, not anonymous browser clicks.
6. Enable secure inputs/outputs on actions containing documents or delegated Graph tokens.
   Teams/email completion delivery is the explicit notify stub TODO, not a shipped connector.

### SharePoint / OneDrive

`POST /jobs/from-url` accepts `{ "url": "<file-sharing-url>", "targetLanguage": "en", ... }`.
Set `SHAREPOINT_HOSTS` to exact tenant hosts (including `<tenant>-my.sharepoint.com` if needed).
The API resolves the link with Microsoft Graph `/shares/{encoded-url}/driveItem`, downloads with
bounded size, and never accepts arbitrary HTTP/download hosts.

Delegated option: the flow obtains a **Graph** token for the same signed-in user and passes it in
`X-Graph-Token` alongside the separate API `Authorization` token. Configure Graph delegated
`Files.ReadWrite` (the `/shares` API's documented delegated minimum; use tenant consent policy).
Do not pass the API token as a Graph token. The token is used only during intake, never persisted
in the job/orchestration. Power Automate custom-connector/flow settings must hide secure headers.

Application option: omit `X-Graph-Token`. The Function MI requests a Graph token; a tenant
administrator must grant Graph application permission `Files.ReadWrite.All`, required by the
sharing-link API. This is broad: use a dedicated POC tenant/site and review with the tenant admin.
Do not claim `Sites.Selected` works with arbitrary sharing-link resolution. A narrower
site/drive/item-based API integration would need a separate endpoint and permission design.

## Workflow behavior

Precheck rejects encrypted/copy-restricted PDFs, malformed documents and oversized/expanded
PPTX archives. Prepare extracts PDF text using Document Intelligence `prebuilt-layout`; native
PPTX uses python-pptx. Image-bearing PPTX is rendered and each slide OCR'd with `prebuilt-read`
on **both source and target**, so native text is not double-counted. Missing renderer configuration
is a visible failed job, not an image-text success fallback.

Term-guard sees approximately 6k-token chunks with 200-token overlap. Proposed terms must occur
verbatim in the source, are deduplicated, sorted longest first and capped at 500. Approved mappings
override proposals. A per-job two-column TSV combines approved/keep/map terms; maximum 10 MiB.
The tri-language standard glossary is bundled, not sent directly to Translator as a three-column TSV.

Translator uses the custom-subdomain endpoint, Entra auth and service MI blob access.
Batch polling uses Durable timers (2, 4, 8, 16, then at most 30 seconds), a 30-minute deadline,
and per-document statuses/cost counters. Refreshed tokens are acquired per request.
The API's published schema puts image translation at top-level
`options.translateTextWithinImage`, **not** inside an individual input/target.

Pass A normalizes locale-specific numeric dates, numbers, EUR/USD/GBP amounts, percentages,
configured IDs, emails and international `+` phone numbers, comparing multisets in both directions.
Keep terms require exact occurrences; mapped terms require presence. Pass B aligns PPTX by slide
and PDF by Document Intelligence paragraph roles/reading order, not page number. It sends chunked
pairs, glossary and deterministic findings to the pinned translation-checker agent.

Final score is `min(minimum reviewer chunk score, 100 - weighted unmatched penalties)`, bounded
at zero. Default pass threshold is 85; any critical reviewer issue forces review.
Configure `PASS_A_WEIGHTS_JSON`, `PASS_THRESHOLD`, `ID_REGEXES_JSON` and optional locale overrides.
Flagged documents get a header on every PDF page or footer on every slide; Translator output
remains immutable and the reviewed copy is stored in `reports`. Notify is deliberately a status
update stub; Teams/email integration is an explicit TODO.

Table `jobs` records state, ownership, timestamps, scores, errors, usage and step durations.
Reports contain both passes, reviewer/model/prompt versions and cost signals. Document text,
agent JSON and excerpts stay in private blobs, not Durable history or logs. Application Insights
messages carry `jobId`. Default SDK HTTP body logging and GenAI content tracing remain disabled.
An ambiguous crash between Translator accepting POST and persisting the operation can leave an
orphan batch; automatic POST retries are deliberately disabled. Inspect the job before resubmitting.

## Evaluation

```bash
PYTHONPATH=src:. python scripts/translator_spike.py
# A: Translator only, B: customer outputs named like the samples, C: full HTTP pipeline
PYTHONPATH=src:. python eval/run_eval.py --output eval/results.csv
# Reverse-direction spike using your own English input:
PYTHONPATH=src:. python scripts/translator_spike.py --file /path/english.pdf --source en --target it
```

Put customer baseline PDFs/PPTX in `eval/baseline/` under the original sample filenames.
The directory is ignored to avoid committing customer documents. Missing files produce explicit
`baseline_missing` CSV rows. All available arms use the same reviewer and seeded oracle.
For flagged C jobs, the evaluator reads the original private Translator output after verifying
the protected result endpoint, so the required review banner is not scored as a translation addition.
CSV includes per-document score, counts by issue type, Pass A misses against seeded values,
latency, characters, images, pipeline tokens and separate evaluation tokens. Baseline historical
latency/usage is unknown and remains blank, not fabricated as zero. The live evaluation
requires an Entra identity with API scope/role and permission to call the pinned Foundry agents.

## Limitations and acceptance boundaries

* **PDF mode:** the published 2026-03-01 OpenAPI does not define the previously proposed
  `pdfDocumentIntelligence`/`useDocumentIntelligence` flags. No invented field is sent. Verify
  whether Document Intelligence PDF processing is automatic in your resource/API or requires an
  additional documented capability; the live PDF spike remains an acceptance gate.
* Region/model/quota availability, RBAC propagation, Graph admin consent and actual tenant
  deployment cannot be validated offline. `azd up` provisions the app resources but Entra registrations,
  renderer consent and agent versions require the checklist steps.
* PDF paragraph-role alignment is heuristic, not semantic alignment; heavily reorganized
  sections, ambiguous or named-month dates and OCR failures require human review. EUR/USD/GBP
  are the current deterministic currency set; extend patterns for other currencies.
* OCR is not infallible; LibreOffice may substitute fonts or alter formatting. It processes untrusted
  documents: keep its container patched, resource-limited and restrict network egress in production.
* Fourteen-day blob lifecycle rules cover fixed containers. A daily identity-authenticated timer
  deletes old dynamic `target-{jobId}` containers and associated artifacts. Table job summaries,
  Durable history and Foundry service retention require a separate approved retention policy;
  they are not represented as covered by the blob rules.
* Do not store/log raw tokens or enable document-content tracing. This POC does not implement
  private networking everywhere, malware scanning, human approval UI, Teams delivery or billing
  currency estimates. Character/image/token counts are usage signals, not price guarantees.

## API/SDK references

The REST request and response fields are taken from the authoritative
[2026-03-01 Translator OpenAPI](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/translation/data-plane/DocumentTranslation/stable/2026-03-01/openapi.json).
See also [Translator batch API](https://learn.microsoft.com/azure/ai-services/translator/document-translation/latest/rest-api/translate-asynchronous),
[managed identities](https://learn.microsoft.com/azure/ai-services/translator/document-translation/how-to-guides/create-use-managed-identities),
[Foundry SDK](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/ai/azure-ai-projects),
[Document Intelligence SDK](https://github.com/Azure/azure-sdk-for-python/tree/main/sdk/documentintelligence/azure-ai-documentintelligence),
and [Graph sharing links](https://learn.microsoft.com/graph/api/shares-get).
Learn was unreachable in this environment; official generated SDKs/specifications were used instead.
Live compatibility, especially the PDF capability, remains explicitly unverified.
