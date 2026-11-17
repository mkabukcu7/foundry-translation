# SPDX-License-Identifier: MIT
"""Context-aware text translation through a configurable Foundry model deployment."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from services.chunking import encoding
from services.config import ExternalServiceError, integer
from services.foundry_agents import invoke_translation_model

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = {"translator", "direct_gpt", "translator_refine"}
PROMPTS = {
    "direct_gpt": "contextual_translation.md",
    "translator_refine": "translation_refinement.md",
}


class TranslationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    decision: str
    rationale: str


class TranslationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translations: list[TranslationItem]


def _document_outline(segments, indexes):
    first, last = indexes[0], indexes[-1]
    outline_indexes = set(range(min(3, len(segments))))
    outline_indexes.update(range(max(0, len(segments) - 3), len(segments)))
    outline_indexes.update(range(max(0, first - 2), min(len(segments), last + 3)))
    outline = [{
        "location": segment.get("location", f"segment:{index + 1}"),
        "role": segment.get("role", "paragraph"),
        "context": segments[index]["text"][:160],
    } for index in sorted(outline_indexes) for segment in [segments[index]]]
    return outline


def _payload(segments, indexes, terms, strategy, source_language, target_language, job_id, group):
    batch = []
    for index in indexes:
        segment = segments[index]
        item = {
            "id": f"segment-{index + 1}",
            "location": segment.get("location", f"segment:{index + 1}"),
            "role": segment.get("role", "paragraph"),
            "source": segment["text"],
        }
        if strategy == "translator_refine":
            item["translatorOutput"] = segment["translatorOutput"]
        batch.append(item)
    return {
        "jobId": job_id,
        "strategy": strategy,
        "sourceLanguage": source_language,
        "targetLanguage": target_language,
        "group": group,
        "context": _context(segments, indexes, terms),
        "segments": batch,
    }


def _groups(segments, terms, max_tokens, prompt, source_language, target_language, job_id, strategy):
    codec = encoding()
    result, current = [], []
    for index, segment in enumerate(segments):
        group = len(result) + 1
        payload = _payload(
            segments, [*current, index], terms, strategy, source_language, target_language, job_id, group,
        )
        input_tokens = len(codec.encode(prompt)) + len(codec.encode(json.dumps(payload, ensure_ascii=False)))
        if current and input_tokens > max_tokens:
            result.append(current)
            current = []
            group += 1
            payload = _payload(
                segments, [index], terms, strategy, source_language, target_language, job_id, group,
            )
            input_tokens = len(codec.encode(prompt)) + len(codec.encode(json.dumps(payload, ensure_ascii=False)))
        if input_tokens > max_tokens:
            raise ValueError(f"Segment {index + 1} exceeds TRANSLATION_CONTEXT_TOKENS")
        current.append(index)
    if current:
        result.append(current)
    return result


def _context(segments, indexes, terms):
    first, last = indexes[0], indexes[-1]
    neighbors = list(range(max(0, first - 1), first)) + list(range(last + 1, min(len(segments), last + 2)))
    relevant = sorted({*indexes, *neighbors})
    context_text = "\n".join(segments[index]["text"] for index in relevant)
    local_terms = [term for term in terms if term["term"] in context_text]
    return {
        "documentContext": _document_outline(segments, indexes),
        "surroundingSegments": [{
            "location": segments[index].get("location", f"segment:{index + 1}"),
            "role": segments[index].get("role", "paragraph"),
            "text": segments[index]["text"],
        } for index in neighbors],
        "terms": local_terms,
    }


def translate(segments, source_language, target_language, terms, job_id, *, strategy):
    if strategy not in PROMPTS:
        raise ValueError("LLM translation requires direct_gpt or translator_refine strategy")
    if not segments:
        raise ValueError("No source segments to translate")

    prompt = (ROOT / "prompts" / PROMPTS[strategy]).read_text(encoding="utf-8")
    max_tokens = integer("TRANSLATION_CONTEXT_TOKENS", 3000)
    groups = _groups(
        segments, terms, max_tokens, prompt, source_language, target_language, job_id, strategy,
    )
    output = [None] * len(segments)
    usage = []

    for group_number, indexes in enumerate(groups, 1):
        payload = _payload(
            segments, indexes, terms, strategy, source_language, target_language, job_id, group_number,
        )
        batch = payload["segments"]
        try:
            response, cost = invoke_translation_model(payload, prompt, job_id)
            parsed = TranslationResponse.model_validate(response)
        except ValidationError as error:
            raise ExternalServiceError("Foundry translation returned an invalid response") from error

        expected = {item["id"]: item for item in batch}
        received = [item.id for item in parsed.translations]
        if len(received) != len(set(received)) or set(received) != set(expected):
            raise ExternalServiceError("Foundry translation did not return exactly one result per segment")
        for item in parsed.translations:
            if not item.text.strip():
                raise ExternalServiceError("Foundry translation returned empty text")
            original = expected[item.id]
            if strategy == "direct_gpt":
                if item.decision != "translated":
                    raise ExternalServiceError("Foundry direct translation returned an unexpected decision")
            elif item.decision == "unchanged":
                if item.text != original["translatorOutput"]:
                    raise ExternalServiceError("Foundry refinement marked changed text as unchanged")
            elif item.decision == "refined":
                if item.text == original["translatorOutput"] or not item.rationale.strip():
                    raise ExternalServiceError("Foundry refinement did not provide a justified change")
            else:
                raise ExternalServiceError("Foundry refinement returned an unexpected decision")
            output[int(item.id.removeprefix("segment-")) - 1] = {
                key: value for key, value in segments[int(item.id.removeprefix("segment-")) - 1].items()
                if key != "translatorOutput"
            } | {"text": item.text}

        usage.append({
            **cost,
            "group": group_number,
            "segmentCount": len(batch),
            "refinedCount": sum(item.decision == "refined" for item in parsed.translations),
        })

    if any(segment is None for segment in output):
        raise ExternalServiceError("Foundry translation omitted one or more source segments")
    return {"segments": output, "language": target_language}, {
        "jobId": job_id,
        "model": usage[0]["model"],
        "agent": usage[0].get("agent"),
        "version": usage[0].get("version"),
        "responseModel": usage[0]["responseModel"],
        "inputTokens": sum(item["inputTokens"] for item in usage),
        "outputTokens": sum(item["outputTokens"] for item in usage),
        "durationSeconds": sum(item["durationSeconds"] for item in usage),
        "calls": usage,
        "strategy": strategy,
    }
