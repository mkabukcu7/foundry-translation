import pytest

from services import contextual_translation
from services.config import ExternalServiceError


class CharacterEncoding:
    @staticmethod
    def encode(text):
        return list(text)


def invoke_payloads(monkeypatch, response_factory):
    seen = []

    def invoke(payload, _prompt, job_id):
        seen.append((payload, job_id))
        return response_factory(payload), {
            "model": "gpt-6-sol", "responseModel": "gpt-6-sol",
            "inputTokens": 12, "outputTokens": 8, "durationSeconds": 0.1,
        }

    monkeypatch.setattr(contextual_translation, "invoke_translation_model", invoke)
    monkeypatch.setattr(contextual_translation, "encoding", lambda: CharacterEncoding())
    return seen


def test_direct_translation_sends_context_and_validates_segment_ids(monkeypatch):
    seen = invoke_payloads(monkeypatch, lambda payload: {
        "translations": [{
            "id": segment["id"], "text": f"Translated {segment['source']}",
            "decision": "translated", "rationale": "Glossary applied",
        } for segment in payload["segments"]],
    })
    monkeypatch.setenv("TRANSLATION_CONTEXT_TOKENS", "3000")
    segments = [
        {"text": "Il premio aumenta.", "role": "paragraph", "location": "paragraph:1"},
        {"text": "La polizza protegge il premio.", "role": "paragraph", "location": "paragraph:2"},
        {"text": "Fine.", "role": "paragraph", "location": "paragraph:3"},
    ]
    terms = [{"term": "premio", "type": "insurance_term", "action": "map", "target": "premium"}]

    result, usage = contextual_translation.translate(
        segments, "it", "en", terms, "job-1", strategy="direct_gpt",
    )

    payload, job_id = seen[0]
    assert job_id == "job-1"
    assert payload["sourceLanguage"] == "it" and payload["targetLanguage"] == "en"
    assert payload["segments"][1]["location"] == "paragraph:2"
    assert any(item["context"] == "Il premio aumenta." for item in payload["context"]["documentContext"])
    assert payload["context"]["terms"] == terms
    assert result["segments"][1]["text"] == "Translated La polizza protegge il premio."
    assert usage["model"] == "gpt-6-sol" and usage["inputTokens"] == 12


def test_refinement_preserves_and_changes_only_explicitly_marked_items(monkeypatch):
    invoke_payloads(monkeypatch, lambda _payload: {"translations": [
        {"id": "segment-1", "text": "The policy expires.", "decision": "unchanged",
         "rationale": "Already accurate in context"},
        {"id": "segment-2", "text": "The premium increases.", "decision": "refined",
         "rationale": "Uses the glossary term for premio"},
    ]})
    segments = [
        {"text": "La polizza scade.", "translatorOutput": "The policy expires.", "location": "slide:1"},
        {"text": "Il premio aumenta.", "translatorOutput": "The reward increases.", "location": "slide:1"},
    ]

    result, _ = contextual_translation.translate(
        segments, "it", "en", [], "job-2", strategy="translator_refine",
    )

    assert [segment["text"] for segment in result["segments"]] == [
        "The policy expires.", "The premium increases.",
    ]
    assert all("translatorOutput" not in segment for segment in result["segments"])


@pytest.mark.parametrize("response", [
    {"translations": [{"id": "segment-2", "text": "x", "decision": "translated", "rationale": ""}]},
    {"translations": [{"id": "segment-1", "text": "x", "decision": "translated"}]},
])
def test_rejects_missing_or_invalid_model_results(monkeypatch, response):
    invoke_payloads(monkeypatch, lambda _payload: response)

    with pytest.raises(ExternalServiceError):
        contextual_translation.translate(
            [{"text": "source"}], "it", "en", [], "job-3", strategy="direct_gpt",
        )


def test_refinement_must_not_mark_changed_output_unchanged(monkeypatch):
    invoke_payloads(monkeypatch, lambda _payload: {"translations": [
        {"id": "segment-1", "text": "Reworded text", "decision": "unchanged", "rationale": "Style"},
    ]})

    with pytest.raises(ExternalServiceError, match="marked changed text"):
        contextual_translation.translate(
            [{"text": "source", "translatorOutput": "Original wording"}],
            "it", "en", [], "job-4", strategy="translator_refine",
        )


def test_rejects_empty_translation_text(monkeypatch):
    invoke_payloads(monkeypatch, lambda _payload: {"translations": [
        {"id": "segment-1", "text": "  ", "decision": "translated", "rationale": ""},
    ]})

    with pytest.raises(ExternalServiceError, match="empty text"):
        contextual_translation.translate(
            [{"text": "source"}], "it", "en", [], "job-5", strategy="direct_gpt",
        )
