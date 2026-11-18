# SPDX-License-Identifier: MIT
import csv
import io
import re
from collections import Counter
from math import sqrt

from babel import Locale

from services.config import integer
from services.models import Term

LOCALES = {"it": "it_IT", "es": "es_ES", "en": "en_US"}


def locale_for(language):
    return LOCALES.get(language) or str(Locale.parse(language.replace("-", "_")))


def standard_terms(data: str, source_language: str, target_language: str, text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(data), delimiter="\t"))
    source_language, target_language = source_language[:2], target_language[:2]
    if target_language not in LOCALES:
        raise ValueError("Standard glossary supports en, it and es targets")
    if source_language not in LOCALES:
        return []  # Coverage is explicitly recorded in the prepared artifact/report.
    return [
        {"term": r[source_language], "type": "insurance_term", "action": "map", "target": r[target_language]}
        for r in rows if r[source_language] in text
    ]


def relevant_terms(data: str, source_language: str, target_language: str, text: str, mode: str) -> list[dict]:
    if mode == "none":
        return []
    if mode not in {"static", "retrieval"}:
        raise ValueError("Glossary mode must be none, static or retrieval")
    if not data.strip():
        raise ValueError("Glossary file is empty")
    delimiter = "\t" if "\t" in data.splitlines()[0] else ","
    reader = csv.DictReader(io.StringIO(data), delimiter=delimiter)
    rows = [
        {str(key).strip().lower(): value for key, value in row.items() if key is not None}
        for row in reader
    ]
    source_language, target_language = source_language[:2], target_language[:2]
    if source_language not in LOCALES or target_language not in LOCALES:
        return []
    headers = {str(header).strip().lower() for header in (reader.fieldnames or [])}
    if source_language not in headers or target_language not in headers:
        raise ValueError(f"Glossary must include {source_language} and {target_language} columns")
    candidates = []
    for row in rows:
        term, target = row.get(source_language), row.get(target_language)
        if not term or not target:
            continue
        if any(character in term or character in target for character in "\t\r\n"):
            raise ValueError("Glossary terms cannot contain tabs or line breaks")
        candidates.append({
            "term": term, "type": "insurance_term", "action": "map", "target": target,
        })
    if mode == "static":
        return [term for term in candidates if term["term"].casefold() in text.casefold()]

    def vector(value):
        return Counter(re.findall(r"\w+", value.casefold()))

    document = vector(text)
    document_norm = sqrt(sum(count * count for count in document.values()))
    if not document_norm:
        return []
    ranked = []
    for term in candidates:
        terms = vector(term["term"])
        norm = sqrt(sum(count * count for count in terms.values()))
        if not norm:
            continue
        similarity = sum(count * document.get(word, 0) for word, count in terms.items()) / (norm * document_norm)
        if similarity > 0:
            ranked.append((similarity, term["term"], term))
    limit = integer("GLOSSARY_RETRIEVAL_LIMIT", 20)
    return [term for _, _, term in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]]


def validate_terms(terms: list[dict], source: str, limit: int = 500) -> list[dict]:
    unique = {}
    for raw in terms:
        term = Term.model_validate(raw)
        if not term.term or term.term not in source:
            continue
        if any(c in term.term or c in (term.target or "") for c in "\t\r\n"):
            continue
        if term.action == "map" and not term.target:
            raise ValueError("Mapped glossary term requires a target")
        # Caller appends approved standard terms last, overriding agent proposals.
        unique[term.term] = term.model_dump()
    return sorted(unique.values(), key=lambda term: (-len(term["term"]), term["term"]))[:limit]


def tsv(terms: list[dict]) -> bytes:
    return "".join(f"{t['term']}\t{t['term'] if t['action'] == 'keep' else t['target']}\n"
                   for t in terms).encode("utf-8")
