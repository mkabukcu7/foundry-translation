# SPDX-License-Identifier: MIT
"""Idempotently create/version Foundry prompt agents. Run with PYTHONPATH=src."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    TextResponseFormatJsonSchema,
)

from services.foundry_agents import definition_files, fingerprint, model_name, project

ROOT = Path(__file__).resolve().parents[1]

TRANSLATION_AGENTS = {
    "contextual-translation-gpt": {
        "model_setting": "TRANSLATION_GPT_AGENT_MODEL_NAME",
        "default_model": "gpt-5.4-mini",
        "description": (
            "Context-aware document translation comparison agent using the cost-effective "
            "GPT-5.4 mini deployment. Uses surrounding document context and glossary terms "
            "while preserving meaning, names, acronyms, numbers, and protected terms."
        ),
    },
    "contextual-translation-mistral": {
        "model_setting": "TRANSLATION_MISTRAL_AGENT_MODEL_NAME",
        "default_model": "translation-mistral",
        "description": (
            "Context-aware document translation comparison agent using the Mistral Large 3 "
            "open-weight model deployment. Uses surrounding document context and glossary "
            "terms while preserving meaning, names, acronyms, numbers, and protected terms."
        ),
    },
}


def ensure_agent(client, name):
    digest = fingerprint(name)
    # list_versions for a nonexistent agent can return 404.
    from azure.core.exceptions import ResourceNotFoundError

    try:
        versions = list(client.agents.list_versions(name))
    except ResourceNotFoundError:
        versions = []
    for version in versions:
        if (version.metadata or {}).get("definitionHash") == digest:
            return version.version
    prompt, schema = definition_files(name)
    version = client.agents.create_version(
        agent_name=name, metadata={"definitionHash": digest, "jobId": "agent-setup"},
        definition=PromptAgentDefinition(
            model=model_name(name), instructions=prompt,
            text=PromptAgentDefinitionTextOptions(format=TextResponseFormatJsonSchema(
                name=name.replace("-", "_"), schema=schema, strict=True,
            )),
        ),
    )
    return version.version


def ensure_translation_agent(client, name, config):
    model = os.getenv(config["model_setting"], "").strip() or config["default_model"]
    prompt = (ROOT / "src" / "prompts" / "contextual_translation_agent.md").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "src" / "schemas" / "contextual_translation.schema.json").read_text())
    digest = hashlib.sha256(
        (prompt + json.dumps(schema, sort_keys=True) + model + config["description"]).encode(),
    ).hexdigest()[:16]
    from azure.core.exceptions import ResourceNotFoundError

    try:
        versions = list(client.agents.list_versions(name))
    except ResourceNotFoundError:
        versions = []
    for version in versions:
        if (version.metadata or {}).get("definitionHash") == digest:
            return {"version": version.version, "model": model, "description": config["description"]}
    version = client.agents.create_version(
        agent_name=name,
        metadata={"definitionHash": digest, "jobId": "translation-agent-setup"},
        description=config["description"],
        definition=PromptAgentDefinition(
            model=model,
            instructions=prompt,
            temperature=0,
            text=PromptAgentDefinitionTextOptions(
                format=TextResponseFormatJsonSchema(
                    name="contextual_translation",
                    schema=schema,
                    strict=True,
                ),
            ),
        ),
    )
    return {"version": version.version, "model": model, "description": config["description"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-env", type=Path, help="Write agent version environment assignments")
    parser.add_argument(
        "--create-comparison-agents", action="store_true",
        help="Also create the optional comparison translation agents.",
    )
    args = parser.parse_args()
    client = project()
    versions = {
        "TERM_GUARD_VERSION": ensure_agent(client, "term-guard"),
        "CHECKER_VERSION": ensure_agent(client, "translation-checker"),
    }
    if args.create_comparison_agents:
        versions.update({
            name: ensure_translation_agent(client, name, config)
            for name, config in TRANSLATION_AGENTS.items()
        })
    if args.write_env:
        versions_to_write = {key: value for key, value in versions.items()
                             if key in {"TERM_GUARD_VERSION", "CHECKER_VERSION"}}
        args.write_env.write_text("".join(f"{key}={value}\n" for key, value in versions_to_write.items()))
    print(json.dumps(versions))


if __name__ == "__main__":
    main()
