# SPDX-License-Identifier: MIT
"""Environment-only configuration; cloud dependencies are lazy."""

import json
import os
from functools import lru_cache

from azure.identity import DefaultAzureCredential


class ConfigurationError(RuntimeError):
    """A safe-to-display configuration error containing names, never secret values."""


class ExternalServiceError(RuntimeError):
    """A controlled upstream error message containing status codes, never response bodies."""


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing configuration: {name}")
    return value


def integer(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer") from error
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}")
    return value


def id_patterns() -> list[str]:
    return json.loads(os.getenv(
        "ID_REGEXES_JSON",
        r'["(?i)\\b(?:POL|SIN|SIE|CLM)\\s*-\\s*[A-Z0-9]+(?:\\s*-\\s*[A-Z0-9]+)*\\b"]',
    ))


@lru_cache
def credential():
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)
