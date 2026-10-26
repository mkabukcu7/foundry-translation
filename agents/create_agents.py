# SPDX-License-Identifier: MIT
"""Idempotently create/version both Foundry prompt agents. Run with PYTHONPATH=src."""

import argparse
import json
from pathlib import Path

from azure.ai.projects.models import (
    PromptAgentDefinition,
    PromptAgentDefinitionTextOptions,
    TextResponseFormatJsonSchema,
)

from services.config import required
from services.foundry_agents import definition_files, fingerprint, project


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
            model=required("FOUNDRY_MODEL_NAME"), instructions=prompt,
            text=PromptAgentDefinitionTextOptions(format=TextResponseFormatJsonSchema(
                name=name.replace("-", "_"), schema=schema, strict=True,
            )),
        ),
    )
    return version.version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-env", type=Path, help="Write agent version environment assignments")
    args = parser.parse_args()
    versions = {
        "TERM_GUARD_VERSION": ensure_agent(project(), "term-guard"),
        "CHECKER_VERSION": ensure_agent(project(), "translation-checker"),
    }
    if args.write_env:
        args.write_env.write_text("".join(f"{key}={value}\n" for key, value in versions.items()))
    print(json.dumps(versions))


if __name__ == "__main__":
    main()
