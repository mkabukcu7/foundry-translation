# SPDX-License-Identifier: MIT
import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from time import perf_counter

from azure.ai.projects import AIProjectClient
from azure.identity import get_bearer_token_provider
from openai import APITimeoutError, OpenAI

from services.config import (
    ConfigurationError,
    ExternalServiceError,
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
    return os.getenv(setting, "").strip() or required("FOUNDRY_MODEL_NAME")


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
    result = MODELS[name].model_validate_json(response.output_text).model_dump()
    usage = response.usage
    return result, {
        "agent": name, "version": version, "promptVersion": fingerprint(name),
        "jobId": job_id, "model": model_name(name),
        "responseModel": response.model,
        "inputTokens": usage.input_tokens if usage else 0,
        "outputTokens": usage.output_tokens if usage else 0,
        "durationSeconds": perf_counter() - started,
    }


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
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt + "\nJSON schema:\n" + json.dumps(schema)},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=4096,
            extra_headers={"x-ms-client-request-id": job_id},
        )
    if not response.choices or response.choices[0].finish_reason != "stop":
        raise ExternalServiceError("Secondary checker did not complete a review")
    content = response.choices[0].message.content
    if not content:
        raise ExternalServiceError("Secondary checker returned no review")
    result = Review.model_validate_json(content).model_dump()
    usage = response.usage
    return result, {
        "jobId": job_id, "agent": "translation-checker-secondary", "version": digest,
        "promptVersion": digest, "model": model, "responseModel": response.model,
        "inputTokens": usage.prompt_tokens if usage else 0,
        "outputTokens": usage.completion_tokens if usage else 0,
        "durationSeconds": perf_counter() - started,
    }


def invoke_translation_model(data: dict, instructions: str, job_id: str) -> tuple[dict, dict]:
    started = perf_counter()
    agent_name = os.getenv("TRANSLATION_AGENT_NAME", "").strip()
    if agent_name and data.get("strategy") != "translator_refine":
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
    model = os.getenv("TRANSLATION_MODEL_NAME", "").strip() or "gpt-6-luna"
    with OpenAI(
        base_url=required("FOUNDRY_MODEL_ENDPOINT"),
        api_key=get_bearer_token_provider(credential(), "https://ai.azure.com/.default"),
        timeout=180, max_retries=0,
    ) as client:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=integer("TRANSLATION_MAX_OUTPUT_TOKENS", 8192),
            extra_headers={"x-ms-client-request-id": job_id},
        )
    if not response.choices or response.choices[0].finish_reason != "stop":
        raise ExternalServiceError("Foundry translation did not complete")
    content = response.choices[0].message.content
    if not content:
        raise ExternalServiceError("Foundry translation returned no result")
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        raise ExternalServiceError("Foundry translation returned invalid JSON") from None
    usage = response.usage
    return result, {
        "jobId": job_id, "agent": "contextual-translation",
        "model": model, "responseModel": response.model,
        "inputTokens": usage.prompt_tokens if usage else 0,
        "outputTokens": usage.completion_tokens if usage else 0,
        "durationSeconds": perf_counter() - started,
    }


def review_pair(data: dict, job_id: str) -> list[tuple[dict, dict]]:
    timeout = integer("CHECKER_TIMEOUT_SECONDS", 600)

    def review(name, call):
        started = perf_counter()
        logger.info("jobId=%s reviewer=%s event=start timeoutSeconds=%s", job_id, name, timeout)
        try:
            result = call()
        except APITimeoutError:
            logger.error("jobId=%s reviewer=%s event=timeout durationSeconds=%.2f timeoutSeconds=%s",
                         job_id, name, perf_counter() - started, timeout)
            raise ReviewTimeoutError(name, timeout) from None
        logger.info("jobId=%s reviewer=%s event=complete durationSeconds=%.2f",
                    job_id, name, perf_counter() - started)
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
