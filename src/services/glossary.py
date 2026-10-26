# SPDX-License-Identifier: MIT
import csv
import io

from babel import Locale

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
