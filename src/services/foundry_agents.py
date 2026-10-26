# SPDX-License-Identifier: MIT
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentReference

from services.config import ConfigurationError, credential, required
from services.models import Review, Terms

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"term-guard": Terms, "translation-checker": Review}


def definition_files(name):
    prompt = (ROOT / "prompts" / f"{name.replace('-', '_')}.md").read_text()
    schema = json.loads((ROOT / "schemas" / f"{'term_guard' if name == 'term-guard' else 'checker'}.schema.json")
                        .read_text())
    return prompt, schema


def fingerprint(name):
    prompt, schema = definition_files(name)
    return hashlib.sha256((prompt + json.dumps(schema, sort_keys=True) + required("FOUNDRY_MODEL_NAME")).encode()
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
    return AgentReference(name=name, version=version).as_dict()


def invoke(name: str, data: dict, job_id: str) -> tuple[dict, dict]:
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
        "model": required("FOUNDRY_MODEL_NAME"),
        "responseModel": response.model,
        "inputTokens": usage.input_tokens if usage else 0,
        "outputTokens": usage.output_tokens if usage else 0,
    }
