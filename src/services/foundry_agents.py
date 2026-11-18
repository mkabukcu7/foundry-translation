# SPDX-License-Identifier: MIT
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from math import isfinite
from pathlib import Path
from random import uniform
from time import perf_counter, sleep

from azure.ai.projects import AIProjectClient
from azure.identity import get_bearer_token_provider
from httpx import ConnectError
from openai import NOT_GIVEN, APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from pydantic import ValidationError

from services.config import (
    ConfigurationError,
    ExternalServiceError,
    ReviewConnectionError,
    ReviewOutputError,
    ReviewRateLimitError,
    ReviewTimeoutError,
    credential,
    integer,
    required,
)
from services.models import Review, Terms

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"term-guard": Terms, "translation-checker": Review}
logger = logging.getLogger(__name__)


def model_name(name: str) -> str:
    setting = {"term-guard": "TERM_GUARD_MODEL_NAME", "translation-checker": "CHECKER_MODEL_NAME"}[name]
    configured = os.getenv(setting, "").strip()
    if configured:
        return configured
    if name == "translation-checker":
        return (
            os.getenv("GPT6_LUNA_DEPLOYMENT_NAME", "").strip()
            or os.getenv("GPT6_LUNA_MODEL_NAME", "").strip()
            or required("FOUNDRY_MODEL_NAME")
        )
    return required("FOUNDRY_MODEL_NAME")


def definition_files(name):
    prompt = (ROOT / "prompts" / f"{name.replace('-', '_')}.md").read_text()
    schema = json.loads((ROOT / "schemas" / f"{'term_guard' if name == 'term-guard' else 'checker'}.schema.json")
                        .read_text())
    return prompt, schema


def fingerprint(name):
    prompt, schema = definition_files(name)
    return hashlib.sha256((prompt + json.dumps(schema, sort_keys=True) + model_name(name)).encode()
                          ).hexdigest()[:16]


@lru_cache
def project():
    return AIProjectClient(required("FOUNDRY_PROJECT_ENDPOINT"), credential(), logging_enable=False)


@lru_cache(maxsize=128)
def verified_reference(name, version, job_id):
    agent = project().agents.get_version(
        name, version, headers={"x-ms-client-request-id": job_id}, logging_enable=False,
    )
    if (agent.metadata or {}).get("definitionHash") != fingerprint(name):
        raise ConfigurationError("Pinned Foundry agent does not match deployed prompts/model; recreate and pin versions")
    return {"type": "agent_reference", "name": name, "version": version}


def _validate_review(content: str, reviewer: str, job_id: str, attempt_usage: dict | None = None) -> dict:
    try:
        return Review.model_validate_json(content).model_dump()
    except ValidationError as error:
        codes = sorted({item["type"] for item in error.errors(
            include_input=False, include_context=False, include_url=False,
        )})
        usage = attempt_usage or {}
        logger.warning(
            "jobId=%s reviewer=%s event=invalidReview codes=%s outputLength=%s inputTokens=%s outputTokens=%s",
            job_id, reviewer, ",".join(codes), len(content), usage.get("inputTokens"), usage.get("outputTokens"),
        )
        raise ReviewOutputError(reviewer, attempt_usage) from None


def invoke(name: str, data: dict, job_id: str) -> tuple[dict, dict]:
    started = perf_counter()
    version = required("TERM_GUARD_VERSION" if name == "term-guard" else "CHECKER_VERSION")
    # Pin explicit agent versions; runtime must never implicitly create/update agents.
    options = ({"timeout": integer("CHECKER_TIMEOUT_SECONDS", 600), "max_retries": 0}
               if name == "translation-checker" else {})
    with project().get_openai_client(**options) as client:
        response = client.responses.create(
            input=[{"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
            extra_body={"agent_reference": verified_reference(name, version, job_id)},
            metadata={"jobId": job_id},
            store=False,
        )
    usage = response.usage
    call_usage = {
        "agent": name, "version": version, "promptVersion": fingerprint(name),
        "jobId": job_id, "model": model_name(name),
        "responseModel": response.model,
        "inputTokens": usage.input_tokens if usage else 0,
        "outputTokens": usage.output_tokens if usage else 0,
        "durationSeconds": perf_counter() - started,
    }
    result = (_validate_review(response.output_text, name, job_id, call_usage) if name == "translation-checker"
              else MODELS[name].model_validate_json(response.output_text).model_dump())
    return result, call_usage


def invoke_secondary(data: dict, job_id: str) -> tuple[dict, dict]:
    started = perf_counter()
    model = required("MISTRAL_CHECKER_MODEL_NAME")
    prompt, schema = definition_files("translation-checker")
    digest = hashlib.sha256((prompt + json.dumps(schema, sort_keys=True) + model).encode()).hexdigest()[:16]
    with OpenAI(
        base_url=required("FOUNDRY_MODEL_ENDPOINT"),
        api_key=get_bearer_token_provider(credential(), "https://ai.azure.com/.default"),
        timeout=integer("CHECKER_TIMEOUT_SECONDS", 600), max_retries=0,
    ) as client:
        effort = os.getenv("CHECKER_REASONING_EFFORT", "").strip()
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": prompt + "\nJSON schema:\n" + json.dumps(schema)},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            text={"format": {"type": "json_object"}},
            reasoning={"effort": effort} if effort else NOT_GIVEN,
            max_output_tokens=4096,
            extra_headers={"x-ms-client-request-id": job_id},
            metadata={"jobId": job_id},
            store=False,
        )
    if response.status != "completed" or not response.output_text:
        raise ExternalServiceError("Secondary checker did not complete a review")
    content = response.output_text.strip()
    lines = content.splitlines()
    if len(lines) >= 3 and lines[0] in {"```json", "```"} and lines[-1] == "```":
        logger.warning("jobId=%s event=secondaryReviewJsonFence outputLength=%s", job_id, len(content))
        content = "\n".join(lines[1:-1])
    usage = response.usage
    details = getattr(usage, "output_tokens_details", None) if usage else None
    call_usage = {
        "jobId": job_id, "agent": "translation-checker-secondary", "version": digest,
        "promptVersion": digest, "model": model, "responseModel": response.model,
        "reasoningEffort": effort or None,
        "inputTokens": usage.input_tokens if usage else 0,
        "outputTokens": usage.output_tokens if usage else 0,
        "reasoningTokens": getattr(details, "reasoning_tokens", 0) if details else 0,
        "durationSeconds": perf_counter() - started,
    }
    return _validate_review(content, "translation-checker-secondary", job_id, call_usage), call_usage


def invoke_translation_model(
    data: dict,
    instructions: str,
    job_id: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[dict, dict]:
    started = perf_counter()
    agent_name = os.getenv("TRANSLATION_AGENT_NAME", "").strip()
    if agent_name and model is None and data.get("strategy") != "translator_refine":
        version = required("TRANSLATION_AGENT_VERSION")
        agent = project().agents.get_version(
            agent_name, version, headers={"x-ms-client-request-id": job_id}, logging_enable=False,
        )
        with project().get_openai_client() as client:
            response = client.responses.create(
                input=[{"type": "message", "role": "user", "content": json.dumps(data, ensure_ascii=False)}],
                extra_body={"agent_reference": {
                    "type": "agent_reference", "name": agent_name, "version": version,
                }},
                metadata={"jobId": job_id},
                store=False,
                max_output_tokens=integer("TRANSLATION_MAX_OUTPUT_TOKENS", 8192),
            )
        if response.status != "completed" or not response.output_text:
            raise ExternalServiceError("Foundry translation agent did not complete")
        try:
            result = json.loads(response.output_text)
        except json.JSONDecodeError:
            raise ExternalServiceError("Foundry translation agent returned invalid JSON") from None
        usage = response.usage
        return result, {
            "jobId": job_id, "agent": agent_name, "version": version,
            "model": agent.definition.model, "responseModel": response.model,
            "inputTokens": usage.input_tokens if usage else 0,
            "outputTokens": usage.output_tokens if usage else 0,
            "durationSeconds": perf_counter() - started,
        }
    model = model or os.getenv("TRANSLATION_MODEL_NAME", "").strip() or "gpt-6-luna"
    effort = reasoning_effort or os.getenv("GPT6_LUNA_REASONING_EFFORT", "low").strip()
    with OpenAI(
        base_url=required("FOUNDRY_MODEL_ENDPOINT"),
        api_key=get_bearer_token_provider(credential(), "https://ai.azure.com/.default"),
        timeout=180, max_retries=0,
    ) as client:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            text={"format": {"type": "json_object"}},
            reasoning={"effort": effort},
            max_output_tokens=integer("TRANSLATION_MAX_OUTPUT_TOKENS", 8192),
            extra_headers={"x-ms-client-request-id": job_id},
            metadata={"jobId": job_id},
            store=False,
        )
    if response.status != "completed" or not response.output_text:
        raise ExternalServiceError("Foundry translation did not complete")
    content = response.output_text
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise ExternalServiceError("Foundry translation returned invalid JSON") from None
    usage = response.usage
    details = getattr(usage, "output_tokens_details", None) if usage else None
    return result, {
        "jobId": job_id, "agent": "contextual-translation",
        "model": model, "responseModel": response.model,
        "reasoningEffort": effort,
        "inputTokens": usage.input_tokens if usage else 0,
        "outputTokens": usage.output_tokens if usage else 0,
        "reasoningTokens": getattr(details, "reasoning_tokens", 0) if details else 0,
        "durationSeconds": perf_counter() - started,
    }


def _rate_limit_delay(error: RateLimitError) -> float | None:
    headers = error.response.headers
    if headers.get("x-should-retry", "").strip().lower() == "false":
        return None
    for header, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = headers.get(header)
        if value is None:
            continue
        try:
            seconds = float(value) / divisor
        except ValueError:
            if header != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = (date - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if isfinite(seconds):
            return max(0.0, seconds)
    return uniform(5, 6)


def review_pair(data: dict, job_id: str) -> list[tuple[dict, dict]]:
    timeout = integer("CHECKER_TIMEOUT_SECONDS", 600)

    def review(name, call):
        started = perf_counter()
        invalid_usage = None
        logger.info("jobId=%s reviewer=%s event=start timeoutSeconds=%s", job_id, name, timeout)
        for attempt in (1, 2):
            try:
                result = call()
                break
            except APITimeoutError:
                logger.error("jobId=%s reviewer=%s event=timeout durationSeconds=%.2f timeoutSeconds=%s",
                             job_id, name, perf_counter() - started, timeout)
                raise ReviewTimeoutError(name, timeout) from None
            except APIConnectionError as error:
                if attempt == 1 and isinstance(error.__cause__, ConnectError):
                    logger.warning("jobId=%s reviewer=%s event=retryConnect", job_id, name)
                    continue
                logger.error("jobId=%s reviewer=%s event=connectionFailed attempt=%s", job_id, name, attempt)
                raise ReviewConnectionError(name) from None
            except RateLimitError as error:
                delay = _rate_limit_delay(error) if attempt == 1 else None
                if delay is not None and delay <= 60:
                    logger.warning("jobId=%s reviewer=%s event=retryRateLimit delaySeconds=%.2f",
                                   job_id, name, delay)
                    sleep(delay)
                    continue
                logger.warning("jobId=%s reviewer=%s event=rateLimitFailed attempt=%s", job_id, name, attempt)
                raise ReviewRateLimitError(name) from None
            except ReviewOutputError as error:
                if attempt == 2:
                    raise
                invalid_usage = error.attempt_usage
                logger.warning("jobId=%s reviewer=%s event=retryInvalidReview", job_id, name)
        logger.info("jobId=%s reviewer=%s event=complete durationSeconds=%.2f",
                    job_id, name, perf_counter() - started)
        if invalid_usage is not None:
            verdict, usage = result
            totals = {field: (invalid_usage.get(field) or 0) + (usage.get(field) or 0)
                      for field in ("inputTokens", "outputTokens", "reasoningTokens", "durationSeconds")}
            result = verdict, {**usage, **totals, "invalidReviewAttempt": invalid_usage}
        return result

    if not os.getenv("MISTRAL_CHECKER_MODEL_NAME", "").strip():
        return [review("translation-checker", lambda: invoke("translation-checker", data, job_id))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(review, "translation-checker", lambda: invoke("translation-checker", data, job_id))
        secondary = pool.submit(review, "translation-checker-secondary", lambda: invoke_secondary(data, job_id))
        return [primary.result(), secondary.result()]


def agreement(primary: dict, secondary: dict) -> dict:
    def key(issue):
        return tuple(" ".join(issue[field].split()).casefold() for field in (
            "type", "severity", "location", "source_excerpt", "target_excerpt",
        ))

    left = {key(issue) for issue in primary["issues"]}
    right = {key(issue) for issue in secondary["issues"]}
    union = left | right
    return {
        "matchedIssues": len(left & right), "totalIssues": len(union),
        "issueAgreementRate": len(left & right) / len(union) if union else 1.0,
        "disagreement": left != right,
        "issues": [
            {"key": list(item), "reviewers": [
                name for name, keys in (("primary", left), ("secondary", right)) if item in keys
            ]}
            for item in sorted(union)
        ],
    }
