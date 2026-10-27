# SPDX-License-Identifier: MIT
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from time import perf_counter

from azure.ai.projects import AIProjectClient
from azure.identity import get_bearer_token_provider
from openai import OpenAI

from services.config import ConfigurationError, ExternalServiceError, credential, required
from services.models import Review, Terms

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"term-guard": Terms, "translation-checker": Review}


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
    with project().get_openai_client() as client:
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
        timeout=120, max_retries=0,
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


def review_pair(data: dict, job_id: str) -> list[tuple[dict, dict]]:
    if not os.getenv("MISTRAL_CHECKER_MODEL_NAME", "").strip():
        return [invoke("translation-checker", data, job_id)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(invoke, "translation-checker", data, job_id)
        secondary = pool.submit(invoke_secondary, data, job_id)
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
