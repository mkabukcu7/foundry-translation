# SPDX-License-Identifier: MIT
import os
from collections import Counter

from services import alignment, extraction, foundry_agents, glossary, normalize, scoring, storage
from services.chunking import chunks


def review_documents(job, prepared, translated):
    source_locale = os.getenv("SOURCE_LOCALE") or glossary.locale_for(prepared["language"])
    target_locale = os.getenv("TARGET_LOCALE") or glossary.locale_for(job["targetLanguage"])
    findings = normalize.compare(
        prepared["segments"], translated["segments"], prepared["terms"], source_locale, target_locale,
    )
    pairs = alignment.align(prepared["segments"], translated["segments"], job["fileName"].endswith(".pptx"))
    reviews, usage = [], []
    for pair in pairs:
        # Corresponding subdivisions keep each call bounded while retaining both sides.
        source_chunks = chunks(pair["source"])
        target_chunks = chunks(pair["target"])
        for i in range(max(len(source_chunks), len(target_chunks))):
            source_part = source_chunks[i] if i < len(source_chunks) else ""
            target_part = target_chunks[i] if i < len(target_chunks) else ""
            local_terms = [t for t in prepared["terms"] if t["term"] in source_part
                           or (t.get("target") and t["target"] in target_part)]
            local_findings = [f for f in findings if f["match"] == "unmatched"
                              and (f["item"] in source_part or f["item"] in target_part)]
            grouped = Counter((f["item"], f["type"], f["side"]) for f in local_findings)
            review, cost = foundry_agents.invoke("translation-checker", {
                "location": pair["location"], "part": i + 1,
                "source": source_part, "target": target_part,
                "glossary": local_terms,
                "passA": [{"item": item, "type": kind, "side": side, "count": count, "match": "unmatched"}
                          for (item, kind, side), count in grouped.items()],
            }, job["jobId"])
            reviews.append(review)
            usage.append(cost)
    result = scoring.score(findings, reviews)
    return {
        "jobId": job["jobId"], "passA": findings, "passB": reviews, **result,
        "versions": usage, "prepareUsage": prepared.get("usage", []),
        "standardGlossaryAvailable": prepared.get("standardGlossaryAvailable", True),
        "costSignals": {
            "tokens": sum(u["inputTokens"] + u["outputTokens"] for u in [*usage, *prepared.get("usage", [])]),
        },
    }


def run(job):
    prepared = storage.get_json(job["jobId"], "prepared")
    name, data = storage.target(job)
    translated = extraction.extract(data, name)
    storage.put_json(job["jobId"], "target-extracted", translated)
    report = review_documents(job, prepared, translated)
    translation = storage.get_json(job["jobId"], "translation")
    report["translation"] = translation
    report["costSignals"].update({
        "characters": sum(d.get("characterCharged", 0) for d in translation["documents"]),
        "images": sum(d.get("imageCharged", 0) for d in translation["documents"]),
    })
    storage.put_json(job["jobId"], "report", report)
    return {"score": report["score"], "decision": report["decision"], "tokens": report["costSignals"]["tokens"]}
