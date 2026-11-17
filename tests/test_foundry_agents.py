import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import APITimeoutError

from activities.check import review_documents
from services import foundry_agents
from services.config import ExternalServiceError, ReviewTimeoutError


def issue(**overrides):
    return {
        "severity": "minor", "type": "terminology", "location": "paragraph:1",
        "source_excerpt": "franchigia", "target_excerpt": "excess", "explanation": "Review terminology",
        **overrides,
    }


def test_role_models_fallback_and_fingerprints(monkeypatch):
    monkeypatch.setenv("FOUNDRY_MODEL_NAME", "baseline")
    monkeypatch.delenv("TERM_GUARD_MODEL_NAME", raising=False)
    monkeypatch.delenv("CHECKER_MODEL_NAME", raising=False)
    original = foundry_agents.fingerprint("term-guard")
    checker = foundry_agents.fingerprint("translation-checker")
    monkeypatch.setenv("TERM_GUARD_MODEL_NAME", "luna")
    assert foundry_agents.model_name("term-guard") == "luna"
    assert foundry_agents.fingerprint("term-guard") != original
    assert foundry_agents.fingerprint("translation-checker") == checker


def test_reviewers_run_independently_in_parallel(monkeypatch):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    barrier = threading.Barrier(2)
    data = {"source": "synthetic"}

    def primary(name, payload, job_id):
        assert name == "translation-checker" and payload is data and job_id == "job"
        barrier.wait(timeout=5)
        return {"score": 99, "issues": []}, {"model": "luna"}

    def secondary(payload, job_id):
        assert payload is data and job_id == "job"
        barrier.wait(timeout=5)
        return {"score": 98, "issues": []}, {"model": "mistral"}

    monkeypatch.setattr(foundry_agents, "invoke", primary)
    monkeypatch.setattr(foundry_agents, "invoke_secondary", secondary)
    assert [cost["model"] for _, cost in foundry_agents.review_pair(data, "job")] == ["luna", "mistral"]


def test_secondary_failure_is_not_bypassed(monkeypatch):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setattr(foundry_agents, "invoke", lambda *_: ({"score": 100, "issues": []}, {}))

    def fail(*_):
        raise ExternalServiceError("Secondary checker failed")

    monkeypatch.setattr(foundry_agents, "invoke_secondary", fail)
    with pytest.raises(ExternalServiceError):
        foundry_agents.review_pair({}, "job")


@pytest.mark.parametrize("reviewer", ["translation-checker", "translation-checker-secondary"])
def test_review_timeout_identifies_reviewer_without_leaking_content(monkeypatch, caplog, reviewer):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setenv("CHECKER_TIMEOUT_SECONDS", "450")

    def primary(*_):
        if reviewer == "translation-checker":
            raise APITimeoutError(request=httpx.Request("POST", "https://unit/PRIVATE-CONTENT"))
        return {"score": 100, "issues": []}, {}

    def secondary(*_):
        if reviewer == "translation-checker-secondary":
            raise APITimeoutError(request=httpx.Request("POST", "https://unit/PRIVATE-CONTENT"))
        return {"score": 100, "issues": []}, {}

    primary_mock, secondary_mock = MagicMock(side_effect=primary), MagicMock(side_effect=secondary)
    monkeypatch.setattr(foundry_agents, "invoke", primary_mock)
    monkeypatch.setattr(foundry_agents, "invoke_secondary", secondary_mock)
    with pytest.raises(ReviewTimeoutError, match=reviewer) as error:
        foundry_agents.review_pair({}, "timeout-job")
    assert "450s" in str(error.value)
    assert "timeout-job" in caplog.text and reviewer in caplog.text
    assert "PRIVATE-CONTENT" not in str(error.value) + caplog.text
    primary_mock.assert_called_once()
    secondary_mock.assert_called_once()


def test_primary_checker_timeout_and_no_automatic_retries(monkeypatch):
    monkeypatch.setenv("CHECKER_VERSION", "5")
    monkeypatch.setenv("CHECKER_TIMEOUT_SECONDS", "450")
    project = MagicMock()
    client = project.get_openai_client.return_value.__enter__.return_value
    client.responses.create.return_value = SimpleNamespace(
        output_text='{"score":100,"issues":[]}', usage=None, model="gpt-6-luna",
    )
    monkeypatch.setattr(foundry_agents, "project", lambda: project)
    monkeypatch.setattr(foundry_agents, "verified_reference", lambda *_: {})
    monkeypatch.setattr(foundry_agents, "fingerprint", lambda *_: "test")
    monkeypatch.setattr(foundry_agents, "model_name", lambda *_: "gpt-6-luna")
    foundry_agents.invoke("translation-checker", {}, "job")
    project.get_openai_client.assert_called_once_with(timeout=450, max_retries=0)


def test_translation_model_defaults_to_luna_and_allows_override(monkeypatch):
    monkeypatch.delenv("TRANSLATION_AGENT_NAME", raising=False)
    monkeypatch.delenv("TRANSLATION_MODEL_NAME", raising=False)
    monkeypatch.setattr(foundry_agents, "required", lambda _: "https://unit.services.ai.azure.com/openai/v1/")
    monkeypatch.setattr(foundry_agents, "credential", lambda: object())
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: "test-token")
    client = MagicMock()
    client.__enter__.return_value = client
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        model="response-model",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)

    foundry_agents.invoke_translation_model({}, "instructions", "job")
    assert client.chat.completions.create.call_args.kwargs["model"] == "gpt-6-luna"
    assert client.chat.completions.create.call_args.kwargs["max_completion_tokens"] == 8192
    assert "max_tokens" not in client.chat.completions.create.call_args.kwargs

    monkeypatch.setenv("TRANSLATION_MODEL_NAME", "gpt-6-sol")
    monkeypatch.setenv("TRANSLATION_MAX_OUTPUT_TOKENS", "4096")
    foundry_agents.invoke_translation_model({}, "instructions", "job")
    assert client.chat.completions.create.call_args.kwargs["model"] == "gpt-6-sol"
    assert client.chat.completions.create.call_args.kwargs["max_completion_tokens"] == 4096


def test_translation_uses_pinned_foundry_agent(monkeypatch):
    strategy = "direct_gpt"
    monkeypatch.setenv("TRANSLATION_AGENT_NAME", "contextual-translation-gpt")
    monkeypatch.setenv("TRANSLATION_AGENT_VERSION", "2")
    project = MagicMock()
    project.agents.get_version.return_value = SimpleNamespace(
        definition=SimpleNamespace(model="gpt-6-luna", instructions="Remote agent rules"),
    )
    client = project.get_openai_client.return_value.__enter__.return_value
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output_text='{"translations":[]}', model="gpt-6-luna",
        usage=SimpleNamespace(input_tokens=12, output_tokens=8),
    )
    monkeypatch.setattr(foundry_agents, "project", lambda: project)
    result, usage = foundry_agents.invoke_translation_model({"strategy": strategy}, "Mode rules", "job")
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["extra_body"]["agent_reference"] == {
        "type": "agent_reference", "name": "contextual-translation-gpt", "version": "2",
    }
    assert kwargs["metadata"] == {"jobId": "job"} and kwargs["store"] is False
    assert kwargs["input"][-1]["role"] == "user"
    assert all(message["type"] == "message" for message in kwargs["input"])
    assert "instructions" not in kwargs
    assert result == {"translations": []}
    assert usage["agent"] == "contextual-translation-gpt" and usage["version"] == "2"
    assert usage["model"] == "gpt-6-luna" and usage["inputTokens"] == 12


def test_translation_agent_requires_explicit_version(monkeypatch):
    from services.config import ConfigurationError

    monkeypatch.setenv("TRANSLATION_AGENT_NAME", "contextual-translation-gpt")
    monkeypatch.delenv("TRANSLATION_AGENT_VERSION", raising=False)
    with pytest.raises(ConfigurationError, match="TRANSLATION_AGENT_VERSION"):
        foundry_agents.invoke_translation_model({}, "instructions", "job")


def test_refinement_keeps_model_path_when_translation_agent_selected(monkeypatch):
    monkeypatch.setenv("TRANSLATION_AGENT_NAME", "contextual-translation-gpt")
    monkeypatch.setenv("TRANSLATION_MODEL_NAME", "gpt-6-luna")
    monkeypatch.setattr(foundry_agents, "required", lambda _: "https://unit.services.ai.azure.com/openai/v1/")
    monkeypatch.setattr(foundry_agents, "credential", lambda: object())
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: "test-token")
    client = MagicMock()
    client.__enter__.return_value = client
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
        usage=None, model="gpt-6-luna",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)
    _, usage = foundry_agents.invoke_translation_model({"strategy": "translator_refine"}, "Refine only", "job")
    assert client.chat.completions.create.call_args.kwargs["messages"][0]["content"] == "Refine only"
    assert "version" not in usage


def test_agreement_matches_evidence_not_explanation():
    a = {"score": 90, "issues": [issue()]}
    b = {"score": 91, "issues": [issue(source_excerpt=" FRANCHIGIA ", explanation="Different wording")]}
    result = foundry_agents.agreement(a, b)
    assert result["matchedIssues"] == 1 and not result["disagreement"]
    b["issues"][0]["severity"] = "major"
    assert foundry_agents.agreement(a, b)["disagreement"]


@pytest.mark.parametrize("secondary,expected", [
    ({"score": 99, "issues": [issue()]}, "flagged"),
    ({"score": 99, "issues": []}, "passed"),
    ({"score": 84, "issues": []}, "flagged"),
])
def test_disagreement_routes_to_review_and_counts_both_usages(monkeypatch, secondary, expected):
    monkeypatch.setenv("PASS_THRESHOLD", "85")
    monkeypatch.setattr(foundry_agents, "review_pair", lambda *_: [
        ({"score": 100, "issues": []}, {"inputTokens": 10, "outputTokens": 2}),
        (secondary, {"inputTokens": 20, "outputTokens": 3}),
    ])
    segments = [{"text": "franchigia", "location": "paragraph:1", "role": "paragraph"}]
    result = review_documents(
        {"jobId": "job", "fileName": "demo.pdf", "targetLanguage": "en"},
        {"language": "it", "segments": segments, "terms": []},
        {"segments": segments},
    )
    assert result["decision"] == expected
    assert result["costSignals"]["tokens"] == 35
    assert len(result["passB"]) == 2 and len(result["checkerAgreement"]) == 1


@pytest.mark.parametrize("finish_reason,content", [
    ("length", '{"score":99,"issues":[]}'), ("content_filter", None), ("stop", None),
])
def test_secondary_rejects_incomplete_response(monkeypatch, finish_reason, content):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setenv("FOUNDRY_MODEL_ENDPOINT", "https://unit.services.ai.azure.com/openai/v1/")
    client = MagicMock()
    client.__enter__.return_value = client
    client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content)),
    ])
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(foundry_agents, "OpenAI", constructor)
    monkeypatch.setenv("CHECKER_TIMEOUT_SECONDS", "450")
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: lambda: "test")
    with pytest.raises(ExternalServiceError):
        foundry_agents.invoke_secondary({}, "job")
    assert constructor.call_args.kwargs["timeout"] == 450
    assert constructor.call_args.kwargs["max_retries"] == 0
