# SPDX-License-Identifier: MIT
"""Local-file translation and review; no Blob, Table, Queue, Functions host, or Scheduler."""

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from activities.check import review_documents
from activities.precheck import validate_document
from activities.prepare import prepare_terms
from activities.route import watermark
from services import docintel_client, glossary, local_translation, pptx_extract


def run_file(source: Path, output_dir: Path, source_language="it", target_language="en",
             *, job_id: str | None = None, progress: Callable[[str], None] | None = None,
             translator_only: bool = False) -> dict:
    job_id = job_id or uuid4().hex

    def stage(name):
        if progress is not None:
            progress(name)

    stage("validate")
    data = source.read_bytes()
    validate_document(data, source.name)
    is_pdf = source.suffix.lower() == ".pdf"
    if not is_pdf:
        local_translation.native_pptx_only(data)
    durations = {}
    stage("extract")
    started = perf_counter()
    extracted = (docintel_client.extract(data, job_id=job_id) if is_pdf else pptx_extract.extract(data))
    durations["extract"] = perf_counter() - started
    if not any(s["text"].strip() for s in extracted["segments"]):
        raise ValueError(f"No extractable text; jobId={job_id}")
    started = perf_counter()
    prepared = {**extracted, "terms": [], "usage": []}
    if not translator_only:
        stage("prepare")
        prepared = prepare_terms(extracted, source_language, target_language, job_id)
        durations["prepare"] = perf_counter() - started
    started = perf_counter()
    stage("translate")
    if is_pdf:
        translated, translation = local_translation.translate_segments(
            extracted["segments"], source_language, target_language, job_id,
        )
        result = "\n\n".join(s["text"] for s in translated["segments"]).encode("utf-8")
        filename = "translated.txt"
    else:
        result, translation = local_translation.translate_pptx(
            data, glossary.tsv(prepared["terms"]), source_language, target_language, job_id,
        )
        translated = pptx_extract.extract(result)
        filename = "translated.pptx"
    durations["translate"] = perf_counter() - started
    started = perf_counter()
    if translator_only:
        report = {"jobId": job_id, "decision": "not_reviewed", "score": None, "issues": [],
                  "costSignals": {"tokens": 0}, "passA": [], "passB": [], "checkerAgreement": [],
                  "versions": [], "prepareUsage": []}
    else:
        stage("check")
        report = review_documents(
            {"jobId": job_id, "fileName": source.name, "targetLanguage": target_language}, prepared, translated,
        )
        durations["check"] = perf_counter() - started
    report.update({
        "sourceFile": source.name, "sourceSha256": hashlib.sha256(data).hexdigest(),
        "sourceLanguage": source_language, "targetLanguage": target_language,
        "status": ("not_reviewed" if translator_only else
                   "complete" if report["decision"] == "passed" else "needs_review"),
        "workflow": "translator_only" if translator_only else "assisted",
        "translation": translation, "layoutPreserved": not is_pdf,
        "limitations": (["PDF output is extracted/translated text, not the original PDF layout.",
                         ("No glossary or quality review is performed." if translator_only else
                          "The glossary is checked by reviewers, not applied by Translator Text.")] if is_pdf else
                        ["Native-text PPTX only; embedded-image text translation is not supported."]),
    })
    report["costSignals"].update({
        **translation, "ocrPages": extracted.get("pageCount", 0), "durationsSeconds": durations,
    })
    if report["decision"] == "flagged":
        result = (b"Machine translation - requires review\n\n" + result if is_pdf else watermark(result, filename))
    destination = output_dir / job_id
    stage("save")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / filename).write_bytes(result)
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "showcase.json").write_text(json.dumps({
        "sourceText": "\n\n".join(s["text"] for s in extracted["segments"]),
        "translatedText": "\n\n".join(s["text"] for s in translated["segments"]),
        "terms": prepared["terms"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "jobId": job_id, "status": report["status"], "score": report["score"],
        "issueCount": len(report["issues"]), "costSignals": report["costSignals"],
    }))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("samples/results/local"))
    parser.add_argument("--source-language", default="it")
    parser.add_argument("--target-language", default="en")
    args = parser.parse_args()
    job_id = uuid4().hex
    try:
        run_file(args.input, args.output_dir, args.source_language, args.target_language, job_id=job_id)
    except Exception as error:
        print(f"Job failed ({type(error).__name__}); jobId={job_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
