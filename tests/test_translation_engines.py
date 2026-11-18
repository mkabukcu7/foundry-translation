import pytest

from services.config import ConfigurationError
from services.translation_engines import config, estimate_cost, metadata


def test_translator_profile_defaults_to_ga_nmt():
    settings = config("translator")
    assert settings.model_name == "Azure Translator NMT"
    assert settings.api_version == "2026-03-01"
    assert settings.maturity == "GA"
    assert metadata(settings)["engine"] == "translator"


def test_document_llm_profile_is_explicitly_preview(monkeypatch):
    monkeypatch.setenv("DOC_LLM_MODEL_NAME", "gpt-6-luna")
    monkeypatch.setenv("DOC_LLM_DEPLOYMENT_NAME", "eu-luna-doc-translation")
    monkeypatch.setenv("DOC_LLM_DEPLOYMENT_TYPE", "DataZone")
    settings = config("doc_llm")
    assert settings.model_name == "gpt-6-luna"
    assert settings.deployment_name == "eu-luna-doc-translation"
    assert settings.deployment_type == "DataZone"
    assert settings.maturity == "PREVIEW"


def test_document_llm_requires_an_explicit_deployment(monkeypatch):
    monkeypatch.delenv("DOC_LLM_DEPLOYMENT_NAME", raising=False)
    with pytest.raises(ConfigurationError, match="DOC_LLM_DEPLOYMENT_NAME"):
        config("doc_llm")


def test_gpt_profile_defaults_and_reasoning_are_configurable(monkeypatch):
    monkeypatch.setenv("GPT6_1_TERRA_MODEL_NAME", "gpt-6.1-terra")
    monkeypatch.setenv("GPT6_1_TERRA_DEPLOYMENT_NAME", "terra-eu")
    monkeypatch.setenv("GPT6_1_TERRA_DEPLOYMENT_TYPE", "DataZone")
    settings = config("gpt6_1_terra", reasoning_effort="medium")
    assert metadata(settings) == {
        "engine": "gpt6_1_terra", "model": "gpt-6.1-terra",
        "deploymentName": "terra-eu", "deploymentType": "DataZone",
        "modelVersion": None, "apiVersion": "v1", "maturity": "GA",
        "reasoningEffort": "medium",
    }


@pytest.mark.parametrize("engine", ["gpt6_luna", "gpt6_1_terra", "gpt5_6_luna"])
@pytest.mark.parametrize("effort", ["none", "low", "medium"])
def test_supported_poc_reasoning_choices(monkeypatch, engine, effort):
    prefix = {"gpt6_luna": "GPT6_LUNA", "gpt6_1_terra": "GPT6_1_TERRA",
              "gpt5_6_luna": "GPT5_6_LUNA"}[engine]
    monkeypatch.setenv(f"{prefix}_MODEL_NAME", "deployment")
    assert config(engine, reasoning_effort=effort).reasoning_effort == effort


def test_invalid_engine_or_reasoning_is_explicit():
    with pytest.raises(ConfigurationError, match="Unknown translation engine"):
        config("gpt6-sol")
    with pytest.raises(ConfigurationError, match="reasoning_effort"):
        config("gpt6_luna", reasoning_effort="xhigh")


def test_cost_estimates_use_configured_deployment_specific_rates(monkeypatch):
    monkeypatch.setenv("GPT6_LUNA_INPUT_RATE_DATAZONE", "0.1")
    monkeypatch.setenv("GPT6_LUNA_OUTPUT_RATE_DATAZONE", "0.5")
    monkeypatch.setenv("GPT6_LUNA_DEPLOYMENT_TYPE", "DataZone")
    engine = config("gpt6_luna")
    assert estimate_cost(engine, input_tokens=1_000_000, output_tokens=1_000_000) == 0.6


def test_cost_estimate_remains_unknown_without_complete_rates(monkeypatch):
    monkeypatch.setenv("GPT6_LUNA_INPUT_RATE_GLOBAL", "0.1")
    monkeypatch.delenv("GPT6_LUNA_OUTPUT_RATE_GLOBAL", raising=False)
    monkeypatch.setenv("GPT6_LUNA_DEPLOYMENT_TYPE", "Global")
    assert estimate_cost(config("gpt6_luna"), input_tokens=100) is None
