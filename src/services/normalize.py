# SPDX-License-Identifier: MIT
"""Locale-aware multiset checks. No values or excerpts are written to logs."""

import re
from collections import Counter
from datetime import date
from decimal import Decimal

from babel.dates import parse_date
from babel.numbers import NumberFormatError, parse_decimal

from services.config import id_patterns

NUMBER = r"(?<![\w])[-+]?(?:\d{1,3}(?:[\u00a0\u202f ]\d{3})+|\d+)(?:[.,]\d+)*(?![\w])"
DATE = r"\b\d{1,4}[./-]\d{1,2}[./-]\d{2,4}\b"
CURRENCY = r"(?:EUR|USD|GBP|€|\$|£)"
AMOUNT = rf"(?:{CURRENCY}\s*{NUMBER}|{NUMBER}\s*{CURRENCY})"
PATTERNS = {
    "amount": AMOUNT,
    "date": DATE,
    "percentage": rf"{NUMBER}\s*%",
    "email": r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
    "phone": r"(?<!\w)\+\d[\d ()-]{6,}\d(?!\w)",
}


def normalize(item: str, kind: str, locale: str) -> str:
    if kind in {"number", "amount", "percentage"}:
        currency = ""
        for code, symbol in [("EUR", "€"), ("USD", "$"), ("GBP", "£")]:
            if code in item or symbol in item:
                currency = code
        numeric = re.sub(r"EUR|USD|GBP|[€$£%]", "", item).strip()
        numeric = numeric.replace("\u202f", "").replace("\u00a0", "").replace(" ", "")
        value = parse_decimal(numeric, locale=locale, strict=True)
        return currency + ":" + str(Decimal(value).normalize())
    if kind == "date":
        if re.match(r"^\d{4}-", item):
            return date.fromisoformat(item).isoformat()
        return parse_date(item, locale=locale).isoformat()
    if kind in {"id", "phone"}:
        return re.sub(r"\s+|[()]", "", item)
    return item.strip().casefold()


def occurrences(segments: list[dict], locale: str, patterns=None) -> list[dict]:
    result = []
    for segment in segments:
        text = segment["text"]
        reserved = []
        ids = id_patterns() if patterns is None else patterns
        for kind, pattern in [*PATTERNS.items(), *[("id", p) for p in ids]]:
            for match in re.finditer(pattern, text):
                if any(match.start() < end and match.end() > start for start, end in reserved):
                    continue
                item = match.group()
                try:
                    value = normalize(item, kind, locale)
                except (NumberFormatError, ValueError, IndexError):
                    value = "unparseable:" + item
                result.append({"item": item, "value": value, "type": kind, "location": segment["location"]})
                reserved.append(match.span())
        for match in re.finditer(NUMBER, text):
            if any(match.start() < end and match.end() > start for start, end in reserved):
                continue
            try:
                value = normalize(match.group(), "number", locale)
            except NumberFormatError:
                value = "unparseable:" + match.group()
            result.append({"item": match.group(), "value": value, "type": "number",
                           "location": segment["location"]})
    return result


def compare(source, target, terms, source_locale, target_locale, patterns=None):
    source_items, target_items = (occurrences(s, loc, patterns) for s, loc in
                                  [(source, source_locale), (target, target_locale)])
    findings = []
    remaining = list(target_items)
    for item in source_items:
        # A verbatim value is unchanged even if it retains the source locale's punctuation.
        candidates = [t for t in remaining if t["type"] == item["type"]
                      and (t["item"] == item["item"] or t["value"] == item["value"])]
        exact = next((t for t in candidates if t["item"] == item["item"]), None)
        chosen = exact or next(iter(candidates), None)
        findings.append({**item, "match": "exact" if exact else "normalized" if chosen else "unmatched",
                         "side": "source"})
        if chosen:
            remaining.remove(chosen)
    findings.extend({**item, "match": "unmatched", "side": "target"} for item in remaining)
    source_text = "\n".join(s["text"] for s in source)
    target_text = "\n".join(s["text"] for s in target)
    for term in terms:
        expected = term["term"] if term["action"] == "keep" else term["target"]
        if (term["action"] == "keep" and term["type"] in {"amount", "date"}
                and any(f["item"] == term["term"] and f["type"] == term["type"]
                        and f["side"] == "source" for f in findings)):
            # Structured checks already cover value and multiplicity; do not impose an extra literal check.
            continue
        # Exact occurrences for keep terms, including multiplicity.
        count = source_text.count(term["term"]) if term["action"] == "keep" else 1
        mapped = (re.search(r"(?<!\w)" + re.escape(expected) + r"(?!\w)", target_text, re.IGNORECASE)
                  if term["action"] == "map" else None)
        for i in range(count):
            findings.append({
                "item": expected, "type": "keep" if term["action"] == "keep" else "map",
                "location": "document",
                "match": (("exact" if mapped.group() == expected else "normalized") if mapped else "unmatched")
                if term["action"] == "map" else ("exact" if target_text.count(expected) > i else "unmatched"),
                "side": "source",
            })
    return findings


def counts(findings):
    return dict(Counter(f["type"] for f in findings if f["match"] == "unmatched"))
