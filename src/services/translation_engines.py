# SPDX-License-Identifier: MIT
"""POC engine profiles and reported deployment metadata."""

import os
from dataclasses import dataclass
from typing import Literal

from services.config import ConfigurationError

EngineId = Literal["translator", "doc_llm", "gpt6_luna", "gpt6_1_terra", "gpt5_6_luna"]
ENGINES: tuple[EngineId, ...] = ("translator", "doc_llm", "gpt6_luna", "gpt6_1_terra", "gpt5_6_luna")
REASONING_EFFORTS = ("none", "low", "medium")


@dataclass(frozen=True)
class EngineConfig:
    engine: EngineId
    model_name: str
    deployment_name: str
    deployment_type: str
    model_version: str
    api_version: str
    maturity: str
    reasoning_effort: str | None = None


_MODEL_SETTINGS = {
    "gpt6_luna": ("GPT6_LUNA", "GPT6_LUNA_MODEL_NAME", "GPT6_LUNA_DEPLOYMENT_NAME"),
    "gpt6_1_terra": ("GPT6_1_TERRA", "GPT6_1_TERRA_MODEL_NAME", "GPT6_1_TERRA_DEPLOYMENT_NAME"),
    "gpt5_6_luna": ("GPT5_6_LUNA", "GPT5_6_LUNA_MODEL_NAME", "GPT5_6_LUNA_DEPLOYMENT_NAME"),
}


def _value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def config(engine: str, *, reasoning_effort: str | None = None) -> EngineConfig:
    if engine not in ENGINES:
        raise ConfigurationError(f"Unknown translation engine: {engine}")
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ConfigurationError(f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}")

    if engine == "translator":
        return EngineConfig(
            engine="translator",
            model_name="Azure Translator NMT",
            deployment_name=_value("TRANSLATOR_DEPLOYMENT_NAME", "standard-nmt"),
            deployment_type=_value("TRANSLATOR_DEPLOYMENT_TYPE", "Regional"),
            model_version=_value("TRANSLATOR_MODEL_VERSION"),
            api_version=_value("TRANSLATOR_DOCUMENT_API_VERSION", "2026-03-01"),
            maturity="GA",
        )
    if engine == "doc_llm":
        deployment = _value("DOC_LLM_DEPLOYMENT_NAME")
        if not deployment:
            raise ConfigurationError("DOC_LLM_DEPLOYMENT_NAME is required for the preview DOC_LLM engine")
        deployment_type = _value("DOC_LLM_DEPLOYMENT_TYPE", "Global")
        if deployment_type not in {"Global", "DataZone", "Regional"}:
            raise ConfigurationError("DOC_LLM_DEPLOYMENT_TYPE must be Global, DataZone, or Regional")
        return EngineConfig(
            engine="doc_llm",
            model_name=_value("DOC_LLM_MODEL_NAME", deployment),
            deployment_name=deployment,
            deployment_type=_value("DOC_LLM_DEPLOYMENT_TYPE", "Global"),
            model_version=_value("DOC_LLM_MODEL_VERSION"),
            api_version=_value("DOC_LLM_API_VERSION", "2026-03-01"),
            maturity="PREVIEW",
        )

    prefix, model_setting, deployment_setting = _MODEL_SETTINGS[engine]
    legacy_model = _value("TRANSLATION_MODEL_NAME", "gpt-6-luna") if engine == "gpt6_luna" else ""
    model = _value(model_setting, legacy_model)
    if not model:
        raise ConfigurationError(f"{model_setting} is required for the {engine} engine")
    deployment = _value(deployment_setting, model)
    effort = reasoning_effort or _value(f"{prefix}_REASONING_EFFORT", "low")
    if effort not in REASONING_EFFORTS:
        raise ConfigurationError(f"{prefix}_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}")
    deployment_type = _value(f"{prefix}_DEPLOYMENT_TYPE", "Global")
    if deployment_type not in {"Global", "DataZone", "Regional"}:
        raise ConfigurationError(f"{prefix}_DEPLOYMENT_TYPE must be Global, DataZone, or Regional")
    return EngineConfig(
        engine=engine,
        model_name=model,
        deployment_name=deployment,
        deployment_type=deployment_type,
        model_version=_value(f"{prefix}_MODEL_VERSION"),
        api_version=_value("RESPONSES_API_VERSION", "v1"),
        maturity="GA",
        reasoning_effort=effort,
    )


def metadata(engine: EngineConfig) -> dict:
    return {
        "engine": engine.engine,
        "model": engine.model_name,
        "deploymentName": engine.deployment_name,
        "deploymentType": engine.deployment_type,
        "modelVersion": engine.model_version or None,
        "apiVersion": engine.api_version,
        "maturity": engine.maturity,
        "reasoningEffort": engine.reasoning_effort,
    }


def estimate_cost(engine: EngineConfig, *, input_tokens=0, output_tokens=0, characters=None) -> float | None:
    if engine.engine in _MODEL_SETTINGS:
        prefix = _MODEL_SETTINGS[engine.engine][0]
        deployment = engine.deployment_type.upper().replace(" ", "_")
        input_rate = _rate(f"{prefix}_INPUT_RATE_{deployment}")
        output_rate = _rate(f"{prefix}_OUTPUT_RATE_{deployment}")
        if input_rate is None or output_rate is None:
            return None
        return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    if characters is None:
        return None
    prefix = "DOC_LLM" if engine.engine == "doc_llm" else "TRANSLATOR"
    deployment = engine.deployment_type.upper().replace(" ", "_")
    rate = _rate(f"{prefix}_CHARACTER_RATE_{deployment}")
    return characters * rate / 1_000_000 if rate is not None else None


def estimate_role_cost(role: str, usage: dict) -> float | None:
    prefix = {
        "term-guard": "TERM_GUARD",
        "translation-checker": "CHECKER",
        "translation-checker-secondary": "MISTRAL_CHECKER",
    }.get(role)
    if prefix is None:
        return None
    default_deployment = _value("GPT6_LUNA_DEPLOYMENT_TYPE", "Global")
    deployment = _value(f"{prefix}_DEPLOYMENT_TYPE", default_deployment).upper().replace(" ", "_")
    input_rate = _rate(f"{prefix}_INPUT_RATE_{deployment}")
    output_rate = _rate(f"{prefix}_OUTPUT_RATE_{deployment}")
    if input_rate is None or output_rate is None:
        return None
    return (usage.get("inputTokens", 0) * input_rate + usage.get("outputTokens", 0) * output_rate) / 1_000_000


def _rate(name: str) -> float | None:
    value = _value(name)
    if not value:
        return None
    try:
        rate = float(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a nonnegative number") from error
    if rate < 0:
        raise ConfigurationError(f"{name} must be a nonnegative number")
    return rate
