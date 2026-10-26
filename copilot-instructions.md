# Project rules — Aon Document Translation POC

## Working style
- Work in phases. After each phase: summarize what was built, list open questions, and STOP for review.
- Verify every Azure API, SDK method, and API version against current Microsoft Learn docs. If uncertain, stop and report what the docs say before implementing.
- Prefer simple, readable code over abstractions.

## Stack
- Python 3.11, Azure Functions v2 programming model, Durable Functions on Flex Consumption (Durable Task Scheduler backend), azd + Bicep.
- pydantic for schemas, pytest for tests, Babel for locale parsing.
- Azure Translator Document Translation API version 2026-03-01 (batch).
- Azure AI Document Intelligence (prebuilt-layout, prebuilt-read).
- Microsoft Foundry prompt agents via the azure-ai-projects SDK, structured JSON output.

## Security (non-negotiable)
- No keys, SAS tokens, or connection strings. Managed identity + RBAC only; DefaultAzureCredential locally.
- HTTP APIs protected by Entra ID.
- Never log document content — log ids, counts, lengths, timings only.
- No secrets or Microsoft-internal URLs/names in code. Code must be customer-shareable (MIT headers).
- All config via environment variables documented in .env.sample.

## Conventions
- Every log line and Foundry call carries the jobId as correlation id.
- Every Translator batch job uses a unique target container: target-{jobId}.
- Record cost signals per job: characters charged, images translated, tokens per agent call, duration per step.
