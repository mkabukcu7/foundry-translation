import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from activities.check import review_documents
from services import foundry_agents
from services.config import ExternalServiceError


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
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: lambda: "test")
    with pytest.raises(ExternalServiceError):
        foundry_agents.invoke_secondary({}, "job")
