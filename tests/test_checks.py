import json
from pathlib import Path

import pytest

from services import alignment, glossary, normalize, scoring
from services.chunking import chunks


def segments(text):
    return [{"text": text, "location": "paragraph:1", "role": "paragraph"}]


@pytest.mark.parametrize("original,target,kind", [
    ("12.450,75", "12,450.75", "number"),
    ("€ 12.450,75", "EUR 12,450.75", "amount"),
    ("14/03/2025", "03/14/2025", "date"),
    ("1,5 %", "1.5%", "percentage"),
])
def test_locale_normalization(original, target, kind):
    assert normalize.normalize(original, kind, "it_IT") == normalize.normalize(target, kind, "en_US")


def test_multiset_detects_duplicate_loss_and_additions():
    findings = normalize.compare(segments("EUR 100,00 EUR 100,00"), segments("EUR 100.00 USD 200.00"),
                                 [], "it_IT", "en_US")
    assert sum(f["match"] == "unmatched" for f in findings) == 2
    assert {f["side"] for f in findings if f["match"] == "unmatched"} == {"source", "target"}


def test_numbers_are_not_merged_across_whitespace_and_ids_normalize_spaces():
    findings = normalize.compare(segments("10 20 POL-2024-558812"),
                                 segments("10 20 POL - 2024 - 558812"), [], "it_IT", "en_US")
    assert all(f["match"] != "unmatched" for f in findings)
    assert len([f for f in findings if f["type"] == "number"]) == 2
    assert next(f for f in findings if f["type"] == "id")["match"] == "normalized"


def test_exact_keep_and_map():
    terms = [
        {"term": "Giulia Rossetti", "type": "name", "action": "keep", "target": None},
        {"term": "franchigia", "type": "insurance_term", "action": "map", "target": "deductible"},
    ]
    findings = normalize.compare(segments("Giulia Rossetti franchigia"), segments("Julia Rossetti deductible"),
                                 terms, "it_IT", "en_US")
    assert [f["type"] for f in findings if f["match"] == "unmatched"] == ["keep"]


def test_expected_seeded_values_are_preserved_by_identity():
    expected = json.loads(Path("samples/expected.json").read_text())
    for meta in expected.values():
        text = "\n".join(v for key, values in meta.items() if key != "sourceLanguage" for v in values)
        locale = glossary.LOCALES[meta["sourceLanguage"]]
        findings = normalize.compare(segments(text), segments(text), [], locale, locale)
        assert all(f["match"] == "exact" for f in findings)


def test_glossary_verbatim_dedupe_cap_and_approved_override():
    terms = [{"term": x, "type": "other", "action": "keep", "target": None}
             for x in ["POL-123", "invented", "POL", "POL-123"]]
    result = glossary.validate_terms(terms, "POL-123", limit=1)
    assert [r["term"] for r in result] == ["POL-123"]
    assert glossary.tsv(result) == b"POL-123\tPOL-123\n"


def test_critical_and_penalty_override_high_llm_score():
    assert scoring.score([{"match": "unmatched", "type": "id"}], [{"score": 100, "issues": []}])["score"] == 80
    assert scoring.score([], [{"score": 100, "issues": [{"severity": "critical"}]}])["decision"] == "flagged"
    assert scoring.score([], [{"score": 85, "issues": []}])["decision"] == "passed"


def test_invalid_penalties_are_visible_configuration_errors(monkeypatch):
    monkeypatch.setenv("PASS_A_WEIGHTS_JSON", '{"amount": NaN}')
    with pytest.raises(ValueError, match="nonnegative"):
        scoring.score([], [{"score": 100, "issues": []}])


def test_alignment_does_not_use_pdf_pages():
    source = [{"text": "one", "role": "sectionHeading", "location": "paragraph:1"},
              {"text": "two three", "role": "paragraph", "location": "paragraph:2"}]
    target = [{"text": "uno", "role": "sectionHeading", "location": "paragraph:1"},
              {"text": "due", "role": "paragraph", "location": "paragraph:2"},
              {"text": "tre", "role": "paragraph", "location": "paragraph:3"}]
    pairs = alignment.align(source, target)
    assert "two three" in "\n".join(p["source"] for p in pairs)
    assert "tre" in "\n".join(p["target"] for p in pairs)


def test_chunks_overlap_and_cover_unicode():
    parts = chunks("hello world " * 100, size=50, overlap=10)
    assert len(parts) > 1 and all(part for part in parts)
    with pytest.raises(ValueError):
        chunks("x", size=5, overlap=5)
