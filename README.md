# Aon Document Translation POC

Durable Functions workflow: **submit → precheck → prepare → translate → check → route → notify**.
PDF/PPTX input, default source auto-detection and English output, with reverse translation to
Italian or Spanish. Copilot Studio calls an Entra-protected HTTP API.

**Implementation is not evidence of deployment.** Local checks use synthetic files and mocked
Azure boundaries. Live agent smoke tests do not establish full-pipeline acceptance. Complete
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

### Multi-family agents (no hosted agent containers)

Set `TERM_GUARD_MODEL_NAME` and `CHECKER_MODEL_NAME` to the chosen model deployment
names before running the creation helper; each falls back to `FOUNDRY_MODEL_NAME`.
The helper reuses versions only when their prompt/schema/model fingerprints match.
The agents are managed Foundry prompt agents, not hosted custom-code agents.

For the value-oriented demo, use a Luna deployment for both roles. Set
`MISTRAL_CHECKER_MODEL_NAME` to a Mistral-Large-3 deployment and
`FOUNDRY_MODEL_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1/`
to enable independent Chat Completions review with the same Azure credential.
Mistral uses JSON mode plus local schema validation, not an assumed strict-schema
or Prompt Agent capability. Blank secondary model preserves single-checker behavior.

The checkers receive the same input in parallel, never each other's answers.
Reports retain both reviews, token usage, durations, and per-issue agreement.
Agreement uses exact type/severity/location and whitespace/case-normalized excerpts;
it is deliberately conservative, not semantic equivalence. Different excerpts can
therefore trigger review even for the same underlying concern. Any unmatched issue
or pass/flag disagreement forces `flagged` (routed to `needs_review`); scores are
never averaged and upstream failures fail the job rather than bypassing a checker.
Excerpts remain in private reports, not logs.

With the agent configuration and pinned versions loaded in the shell, run:

```bash
PYTHONPATH=src python agents/smoke_test.py
```

This makes billable, keyless calls using synthetic Italian text only. It checks
Term Guard identifier preservation, an accurate translation, and a deliberately
altered amount. Output contains only correlation IDs, counts, timings, and usage.
It is a smoke test, not a document-quality benchmark. No Translator, OCR, storage,
or Durable infrastructure is required for this agent-only test.

On Windows, **Terminal > Run Task > Create and smoke-test translation agents**
loads unquoted `NAME=value` assignments from the ignored `.env`, creates/reuses agent
versions, and runs the test in a visible terminal. Select a Python interpreter with
the declared dependencies installed first, or set `PYTHON_EXECUTABLE` in `.env`
to an absolute interpreter path. **Validate translation agents** runs the
offline suite and focused lint without calling Azure.

**Verify live Foundry agent bindings** reads the remote pinned agent definitions
and deployment metadata, then reruns the synthetic smoke test without creating or
updating agents. It makes billable inference calls. To inspect metadata only, run
`.\agents\verify_agents.ps1` without `-SmokeTest`.

Deploy `renderer/` using `bash renderer/deploy.sh` after its Entra registration is ready, or use
an existing renderer implementing the same contract. Set `SLIDE_RENDER_URL` and
`SLIDE_RENDER_SCOPE` in azd and reprovision. The renderer accepts raw PPTX at `/render`, requires
an Entra token with application role `Renderer.Invoke`, and returns `application/pdf`.
It does not use anonymous conversion endpoints or forward customer documents to third parties.

## Local run and validation

### Storage-free translation first

#### Localhost showcase UI

For a screen-share demonstration, run the VS Code task **Start localhost translation
showcase**, then open **http://localhost:8765**. The task runs in a visible terminal;
stop it with Ctrl+C. It uses the same ignored `.env` and Python environment as the
local-file runner. `requirements-dev.txt` includes FastAPI and Uvicorn through the
existing renderer requirements; no JavaScript build or new Azure resources are needed.

The UI offers three synthetic Italian samples or a local PDF/PPTX upload. Clicking
**Translate & review** starts real, billable Azure calls. It shows actual workflow
stages, source/translated text, Term Guard terms, each reviewer's findings and model
deployment, exact issue agreement, deterministic preservation checks, usage and timings.
Download the translated output or full JSON report. **Saved demonstrations** opens
earlier results without any additional Azure calls, useful for repeatable presentations.
Text/term previews are saved for new runs only; older CLI reports still open.

```powershell
.\scripts\run-showcase.ps1
```

Keep the server bound to `127.0.0.1:8765`. It intentionally has no public/cloud
authentication, rejects non-local Host headers and cross-origin submissions, and
must not be exposed through a tunnel, reverse proxy, or network-facing bind.
One live job runs at a time; closing the page does not cancel in-flight Azure calls.
Uploaded source files are temporary and removed after processing. Outputs, extracted
text, terminology and private reports remain under ignored `samples/results/local/`.
No document content is written to application logs. Failures show the stage and job ID,
not potentially sensitive service exception text.

Use **Validate localhost translation showcase** for the offline suite and focused lint.
The existing PDF/PPTX limitations and experimental scoring caveats below apply equally
to the UI; it displays reviewer evidence, not hidden model reasoning.

#### Parallel Translator-only baseline

Keep the assisted showcase on **http://localhost:8765**, and run **Start Translator-only
baseline (8766)** to open **http://localhost:8766** alongside it:

```powershell
.\scripts\run-showcase.ps1 -TranslatorOnly
```

Both pages offer the same synthetic samples and local upload support. Submit the same
document separately on each page. New reports show the source filename and SHA-256 hash
so you can verify matching inputs. Each page makes separate service requests; this is
not a synchronized experiment, and service outputs/timings can vary between requests.

The baseline calls the same PDF extraction/OCR and Translator adapters but **never calls
Term Guard or either reviewer, submits no glossary and runs no quality checks**. It records
zero agent tokens because there are no agent calls, a null quality score, and
`not_reviewed` status (not a pass). It adds no review watermark. Its outputs/history are
separate under ignored `samples/results/translator-only/`. PDF extraction still uses
Document Intelligence, so "Translator-only" means no Foundry augmentation, not no OCR.

For PDF, assisted mode adds terminology analysis/review but does not apply the glossary
to Translator Text. For PPTX, assisted mode also submits a glossary. Neither page proves
that adding agents automatically improves the translated wording.

#### Review calibration

Deterministic comparisons accept verbatim preserved values even if the target retains
source-locale punctuation. Non-identical values must still normalize under their declared
locales; arbitrary fallback locales are not tried. Amount/date keep terms already covered
by structured checks do not receive a second literal-format penalty. Name/ID protection
remains strict. Mapped glossary terms match case-insensitively at word/phrase boundaries,
so `Policy` matches `policy`, but `policyholder` does not.

The versioned checker instructions require evidence-backed defects, exclude harmless
formatting/article/capitalization preferences, respect approved terminology and retain
substantive number, coverage and singular/plural errors. No findings are silently deleted
after a model responds. Scores and conservative disagreement routing are unchanged.
Small live regressions are not a calibrated accuracy benchmark or a zero-false-positive
guarantee.

After updating prompts, use **Create and smoke-test translation agents** to create/reuse
matching immutable versions, then **Live reviewer calibration regression**. The latter
tests two accurate formatting variants and seeded amount, coverage, policy-ID and
singular/plural defects with both reviewers. It requires the expected defect evidence,
not just any issue, and rejects spurious amount-formatting findings. Calls are billable;
private results go to ignored `samples/results/calibration/`.
Local demo, verification and showcase scripts load `.agent-versions.env` after `.env`,
overriding only the two pinned agent-version settings. Restart both servers after changing
code/prompts/version pins. Saved historical reports retain their original scores/findings
and are never retroactively recalibrated.

Use the local-file runner before provisioning the Durable pipeline. This mode needs
only existing Foundry prompt agents/model deployments, Translator S1 and Document
Intelligence S0. It does **not** use Azure Blob/Table/Queue Storage, a Functions host,
Durable Task Scheduler, or an Entra application registration. It is a local CLI,
not an unauthenticated HTTP API.

- PDF: Document Intelligence extraction/OCR → Term Guard → Translator Text →
  independent reviewers → local UTF-8 text. The original PDF layout is not retained.
  The glossary is reviewed but **not applied** by Translator Text.
- Native-text PPTX: extraction → Term Guard → synchronous Document Translation with
  TSV glossary → independent reviewers → local PPTX. Images are rejected; embedded
  image OCR is outside this mode. Document limit: 10 MiB; glossary limit: 1 MiB.

The synchronous
[supported formats](https://learn.microsoft.com/en-us/azure/ai-services/translator/document-translation/overview)
do not include PDF; this is intentionally a text-first PDF workflow, not an assumed
synchronous PDF layout-preservation feature. PPTX uses the documented
[2024-05-01 endpoint](https://learn.microsoft.com/en-us/azure/ai-services/translator/document-translation/reference/translate-document).
Both service adapters use Entra bearer tokens, never subscription keys.

Put unquoted `NAME=value` settings in the ignored `.env`: `TRANSLATOR_ENDPOINT`,
`DOCINTEL_ENDPOINT`, the Foundry settings and pinned agent versions described above,
and `PYTHON_EXECUTABLE` pointing to an environment with the declared dependencies.
Use custom-subdomain resource origins for both service endpoints. Your local Azure
CLI identity needs data-plane access (Cognitive Services User) on each service.
Keep local authentication disabled on the Azure resources.

In VS Code, run **Validate storage-free workflow** to run offline tests, focused lint
and generate three synthetic Italian inputs. Then run **Run storage-free translation
demo** and select a generated input. Equivalently, from the repository root in PowerShell:

```powershell
.\scripts\validate-local-demo.ps1
.\scripts\run-local-demo.ps1 -InputFile samples\out\local_scanned_it.pdf
.\scripts\run-local-demo.ps1 -InputFile samples\out\local_claim_it.pptx
```

These demo commands make billable Azure calls using `DefaultAzureCredential`.
Outputs and reports stay in ignored `samples/results/local/<jobId>/` directories.
Reports can contain document excerpts; treat them as private local data.
Stdout contains only correlation IDs, counts, usage and timings. Reports record
OCR page count, tokens per Foundry call, translation character usage when returned,
and per-step duration. Unknown charged-character counts remain `null`, not zero.
Flagged text/PPTX outputs are visibly marked as requiring review.

Live smoke verification succeeded for synthetic Italian native PDF, scanned PDF
and native-text PPTX using keyless services. All three routed to `needs_review`:
this proves execution and review routing, **not quality acceptance**. Both reviewers
caught a singular/plural meaning shift in the PPTX. Other flags include strict glossary
matching, locale formatting and reviewer disagreement in those original runs. The
subsequent calibration above addresses known deterministic false positives and tightens
reviewer instructions; old saved reports remain unchanged. Broader quality validation is
still required before treating the score as an acceptance threshold. No automatic
corrections or retranslation are performed.

Verify saved synthetic outputs without more Azure calls (with `PYTHONPATH=src`):
`python scripts/verify_local_demo.py samples/results/local/<jobId>`.
This checks identifiers, name, amount, English coverage wording, reviewer metadata
and review marking. It is not a general-purpose document accuracy evaluator.

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
