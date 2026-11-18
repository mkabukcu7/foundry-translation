import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from openai import NOT_GIVEN, APIConnectionError, APITimeoutError, RateLimitError

from activities.check import review_documents
from services import foundry_agents
from services.config import (
    ExternalServiceError,
    ReviewConnectionError,
    ReviewOutputError,
    ReviewRateLimitError,
    ReviewTimeoutError,
)


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


def test_checker_defaults_to_luna_and_accepts_explicit_terra_deployment(monkeypatch):
    monkeypatch.setenv("FOUNDRY_MODEL_NAME", "baseline")
    monkeypatch.setenv("GPT6_LUNA_DEPLOYMENT_NAME", "luna-reviewer")
    monkeypatch.delenv("CHECKER_MODEL_NAME", raising=False)
    assert foundry_agents.model_name("translation-checker") == "luna-reviewer"
    monkeypatch.setenv("CHECKER_MODEL_NAME", "terra-reviewer")
    assert foundry_agents.model_name("translation-checker") == "terra-reviewer"


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


def test_parallel_chunks_keep_both_reviews_and_document_order(monkeypatch):
    barrier = threading.Barrier(2)
    segments = [{"text": text, "role": "paragraph", "location": f"paragraph:{i}"}
                for i, text in enumerate(("First", "Second", "Third"), 1)]
    usage = {"inputTokens": 1, "outputTokens": 1}

    def review_pair(data, job_id):
        assert job_id == "job"
        if data["location"] != "paragraph:3":
            barrier.wait(timeout=5)
        return [
            ({"score": 100, "issues": []}, {**usage, "agent": reviewer, "location": data["location"]})
            for reviewer in ("primary", "secondary")
        ]

    monkeypatch.setattr(foundry_agents, "review_pair", review_pair)
    report = review_documents(
        {"jobId": "job", "fileName": "demo.pdf", "targetLanguage": "en", "parallelReviewChunks": 2},
        {"language": "it", "segments": segments, "terms": []},
        {"segments": segments},
    )
    assert [(call["location"], call["agent"]) for call in report["versions"]] == [
        (segment["location"], reviewer) for segment in segments for reviewer in ("primary", "secondary")
    ]
    assert [item["location"] for item in report["checkerAgreement"]] == [
        segment["location"] for segment in segments
    ]
    assert report["costSignals"]["tokens"] == 12


@pytest.mark.parametrize("headers,recover,expected_delays", [
    ({"retry-after-ms": "2500", "retry-after": "9"}, True, [2.5]),
    ({"retry-after": "60"}, True, [60]),
    ({"retry-after": "Thu, 01 Jan 1970 00:00:00 GMT"}, True, [0]),
    ({"retry-after": "nan"}, True, [5.5]),
    ({"retry-after": "61"}, False, []),
    ({"x-should-retry": "false"}, False, []),
    ({}, False, [5.5]),
])
def test_review_rate_limit_retries_only_the_throttled_reviewer(
    monkeypatch, caplog, headers, recover, expected_delays,
):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    primary = MagicMock(return_value=({"score": 100, "issues": []}, {}))
    monkeypatch.setattr(foundry_agents, "invoke", primary)
    waits = []
    monkeypatch.setattr(foundry_agents, "sleep", waits.append)
    monkeypatch.setattr(foundry_agents, "uniform", lambda *_: 5.5)
    response = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://unit.test"))
    error = RateLimitError("PRIVATE RESPONSE", response=response, body=None)
    secondary = MagicMock(side_effect=[error, ({"score": 98, "issues": []}, {}) if recover else error])
    monkeypatch.setattr(foundry_agents, "invoke_secondary", secondary)
    if recover:
        assert [result["score"] for result, _ in foundry_agents.review_pair({}, "job")] == [100, 98]
    else:
        with pytest.raises(ReviewRateLimitError, match=r"translation-checker-secondary.*HTTP 429") as failure:
            foundry_agents.review_pair({}, "job")
        assert "PRIVATE RESPONSE" not in str(failure.value)
    primary.assert_called_once()
    assert waits == expected_delays
    assert secondary.call_count == 1 + len(waits)
    assert "PRIVATE RESPONSE" not in caplog.text


def test_secondary_failure_is_not_bypassed(monkeypatch):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setattr(foundry_agents, "invoke", lambda *_: ({"score": 100, "issues": []}, {}))

    def fail(*_):
        raise ExternalServiceError("Secondary checker failed")

    monkeypatch.setattr(foundry_agents, "invoke_secondary", fail)
    with pytest.raises(ExternalServiceError):
        foundry_agents.review_pair({}, "job")


@pytest.mark.parametrize("recover", [True, False])
def test_invalid_review_retries_only_the_affected_reviewer(monkeypatch, caplog, recover):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    primary = MagicMock(return_value=({"score": 100, "issues": []}, {}))
    monkeypatch.setattr(foundry_agents, "invoke", primary)
    responses = iter(['{"score":"PRIVATE RESPONSE","issues":[]}',
                      '{"score":99,"issues":[]}' if recover else 'invalid JSON'])
    usage = {"inputTokens": 12, "outputTokens": 3, "durationSeconds": 0.5}
    secondary = MagicMock(side_effect=lambda _, job_id: (
        foundry_agents._validate_review(next(responses), "translation-checker-secondary", job_id, usage), usage,
    ))
    monkeypatch.setattr(foundry_agents, "invoke_secondary", secondary)
    if recover:
        _, (result, totals) = foundry_agents.review_pair({}, "job")
        assert result["score"] == 99
        assert totals["inputTokens"] == 24 and totals["outputTokens"] == 6
        assert totals["durationSeconds"] == 1 and totals["invalidReviewAttempt"] == usage
    else:
        with pytest.raises(ReviewOutputError, match="translation-checker-secondary") as error:
            foundry_agents.review_pair({}, "job")
        assert "PRIVATE RESPONSE" not in str(error.value)
    primary.assert_called_once()
    assert secondary.call_count == 2
    assert "retryInvalidReview" in caplog.text
    assert "PRIVATE RESPONSE" not in caplog.text


@pytest.mark.parametrize("transport_error,recover,expected_calls", [
    (httpx.ConnectError, True, 2),
    (httpx.ConnectError, False, 2),
    (httpx.ReadError, False, 1),
])
def test_reviewer_only_retries_failed_connection_setup(monkeypatch, caplog, transport_error, recover, expected_calls):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    primary = MagicMock(return_value=({"score": 100, "issues": []}, {}))
    result = ({"score": 99, "issues": []}, {})
    calls = 0

    def secondary(*_):
        nonlocal calls
        calls += 1
        if recover and calls == 2:
            return result
        request = httpx.Request("POST", "https://unit/PRIVATE-ENDPOINT")
        raise APIConnectionError(request=request) from transport_error("PRIVATE-ENDPOINT")

    monkeypatch.setattr(foundry_agents, "invoke", primary)
    monkeypatch.setattr(foundry_agents, "invoke_secondary", secondary)
    if recover:
        assert foundry_agents.review_pair({}, "job") == [primary.return_value, result]
    else:
        with pytest.raises(ReviewConnectionError, match="translation-checker-secondary") as error:
            foundry_agents.review_pair({}, "job")
        assert "PRIVATE-ENDPOINT" not in str(error.value)
    assert calls == expected_calls
    primary.assert_called_once()
    assert "PRIVATE-ENDPOINT" not in caplog.text


@pytest.mark.parametrize("effort", [None, "", "low", "medium"])
def test_secondary_only_sends_explicit_reasoning_effort(monkeypatch, effort):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setenv("FOUNDRY_MODEL_ENDPOINT", "https://unit.services.ai.azure.com/openai/v1/")
    if effort is None:
        monkeypatch.delenv("CHECKER_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("CHECKER_REASONING_EFFORT", effort)
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output_text='{"score":100,"issues":[]}', usage=None, model="mistral",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)
    monkeypatch.setattr(foundry_agents, "credential", lambda: object())
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: lambda: "test")

    _, usage = foundry_agents.invoke_secondary({}, "job")

    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["reasoning"] == ({"effort": effort} if effort else NOT_GIVEN)
    assert usage["reasoningEffort"] == (effort or None)
    assert kwargs["metadata"] == {"jobId": "job"}


@pytest.mark.parametrize("content", [
    '{"score":100,"issues":[]}',
    '```json\n{"score":100,"issues":[]}\n```',
    '```\n{"score":100,"issues":[]}\n```',
])
def test_secondary_validates_json_with_optional_markdown_fence(monkeypatch, content):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setenv("FOUNDRY_MODEL_ENDPOINT", "https://unit.services.ai.azure.com/openai/v1/")
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output_text=content, usage=None, model="mistral",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)
    monkeypatch.setattr(foundry_agents, "credential", lambda: object())
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: lambda: "test")

    review, _ = foundry_agents.invoke_secondary({}, "job")

    assert review == {"score": 100, "issues": []}


@pytest.mark.parametrize("content", [
    'Here is the review:\n{"score":100,"issues":[]}',
    '```json\n{"score":101,"issues":[]}\n```',
    '```json\n{"score":100}\n```',
    '```json\n{"score":100,"issues":[]}\n```\nUntrusted trailing text',
])
def test_secondary_rejects_malformed_or_invalid_fenced_json(monkeypatch, content):
    monkeypatch.setenv("MISTRAL_CHECKER_MODEL_NAME", "mistral")
    monkeypatch.setenv("FOUNDRY_MODEL_ENDPOINT", "https://unit.services.ai.azure.com/openai/v1/")
    client = MagicMock()
    client.__enter__.return_value = client
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output_text=content, usage=None, model="mistral",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)
    monkeypatch.setattr(foundry_agents, "credential", lambda: object())
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: lambda: "test")

    with pytest.raises(ReviewOutputError, match="translation-checker-secondary"):
        foundry_agents.invoke_secondary({}, "job")


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
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output_text="{}",
        usage=SimpleNamespace(input_tokens=3, output_tokens=2, output_tokens_details=None),
        model="response-model",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)

    foundry_agents.invoke_translation_model({}, "instructions", "job")
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["max_output_tokens"] == 8192
    assert kwargs["reasoning"] == {"effort": "low"}
    assert kwargs["metadata"] == {"jobId": "job"} and kwargs["store"] is False
    assert "max_completion_tokens" not in kwargs

    monkeypatch.setenv("TRANSLATION_MODEL_NAME", "gpt-6-sol")
    monkeypatch.setenv("TRANSLATION_MAX_OUTPUT_TOKENS", "4096")
    foundry_agents.invoke_translation_model({}, "instructions", "job", reasoning_effort="medium")
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-6-sol"
    assert kwargs["max_output_tokens"] == 4096
    assert kwargs["reasoning"] == {"effort": "medium"}


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
    client.responses.create.return_value = SimpleNamespace(
        status="completed", output_text="{}",
        usage=SimpleNamespace(input_tokens=0, output_tokens=0, output_tokens_details=None),
        model="gpt-6-luna",
    )
    monkeypatch.setattr(foundry_agents, "OpenAI", lambda **_: client)
    _, usage = foundry_agents.invoke_translation_model({"strategy": "translator_refine"}, "Refine only", "job")
    assert client.responses.create.call_args.kwargs["input"][0]["content"] == "Refine only"
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
    client.responses.create.return_value = SimpleNamespace(
        status="completed" if finish_reason == "stop" else "incomplete",
        output_text=content, model="mistral",
        usage=SimpleNamespace(input_tokens=0, output_tokens=0, output_tokens_details=None),
    )
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr(foundry_agents, "OpenAI", constructor)
    monkeypatch.setenv("CHECKER_TIMEOUT_SECONDS", "450")
    monkeypatch.setattr(foundry_agents, "get_bearer_token_provider", lambda *_: lambda: "test")
    with pytest.raises(ExternalServiceError):
        foundry_agents.invoke_secondary({}, "job")
    assert constructor.call_args.kwargs["timeout"] == 450
    assert constructor.call_args.kwargs["max_retries"] == 0
