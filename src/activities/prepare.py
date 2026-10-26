# SPDX-License-Identifier: MIT
from pathlib import Path

from services import extraction, foundry_agents, glossary, jobs_table, storage
from services.chunking import chunks
from services.config import integer
from services.translator_client import Translator


def run(job):
    extracted = extraction.extract(storage.source(job), job["fileName"])
    text = "\n".join(s["text"] for s in extracted["segments"])
    language = job.get("sourceLanguage") or extracted["language"] or Translator().detect(text, job["jobId"])
    glossary.locale_for(language)
    terms, usage = [], []
    standard = (Path(__file__).resolve().parents[1] / "glossaries" / "aon-standard.tsv").read_text()
    for chunk in chunks(text):
        result, cost = foundry_agents.invoke("term-guard", {
            "text": chunk, "standardGlossary": standard, "sourceLanguage": language,
            "targetLanguage": job["targetLanguage"],
        }, job["jobId"])
        terms.extend(result["terms"])
        usage.append(cost)
    terms.extend(glossary.standard_terms(standard, language, job["targetLanguage"], text))
    terms = glossary.validate_terms(terms, text)
    data = glossary.tsv(terms)
    if len(data) > min(integer("MAX_GLOSSARY_MB", 10), 10) * 1024 * 1024:
        raise ValueError("Prepared glossary exceeds 10 MB maximum")
    storage.put("glossaries", f"{job['jobId']}.tsv", data, "text/tab-separated-values; charset=utf-8")
    storage.put_json(job["jobId"], "prepared", {
        **extracted, "language": language, "terms": terms, "usage": usage,
        "standardGlossaryAvailable": language[:2] in glossary.LOCALES,
    })
    jobs_table.update(job["jobId"], glossaryAvailable=bool(terms))
    return {"sourceLanguage": language, "termCount": len(terms),
            "tokens": sum(u["inputTokens"] + u["outputTokens"] for u in usage)}
