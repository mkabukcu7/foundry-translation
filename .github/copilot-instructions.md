# Project rules — Foundry Translation POC

## Working style
- Follow the user's requested scope and complete all requested phases without pausing for phase-by-phase review.
- Verify Azure APIs, SDK methods, and API versions against current Microsoft Learn documentation or authoritative Azure samples before implementation.
- Prefer simple, readable code over abstractions.

## Stack
- Python 3.11, Azure Functions v2 programming model, Durable Functions on Flex Consumption with Durable Task Scheduler, azd + Bicep.
- Azure Translator Text Translation, Azure AI Document Intelligence, and Microsoft Foundry.

## Security (non-negotiable)
- No keys, SAS tokens, or connection strings. Use managed identity in Azure and DefaultAzureCredential locally.
- HTTP APIs require Entra ID authentication.
- Never log document content; log only identifiers, counts, lengths, and timings.
- No secrets or Microsoft-internal URLs/names in code. Keep code customer-shareable under the MIT license.
- Document configuration through environment variables; do not provision an Entra app registration automatically.

## Conventions
- Every log line and Foundry call carries the jobId as correlation id.
- Every Translator batch job uses a unique `target-{jobId}` container.
- Record cost signals per job: characters charged, images translated, tokens per agent call, and duration per step.
- Keep application locale behavior data-driven; optional source/target locale overrides should remain blank unless explicitly configured.
