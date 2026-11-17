# SPDX-License-Identifier: MIT
"""Local-file translation and review; no Blob, Table, Queue, Functions host, or Scheduler."""

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from activities.check import review_documents
from activities.precheck import validate_document
from activities.prepare import prepare_terms
from activities.route import watermark
from services import (
    contextual_translation,
    docintel_client,
    document_output,
    docx_document,
    glossary,
    local_translation,
    pptx_extract,
    xlsx_document,
)

logger = logging.getLogger(__name__)

def run_file(source: Path, output_dir: Path, source_language="it", target_language="en",
             *, job_id: str | None = None, progress: Callable[[str], None] | None = None,
             translator_only: bool = False, strategy: str = "translator", skip_review: bool = False) -> dict:
    job_id = job_id or uuid4().hex
    if strategy not in contextual_translation.STRATEGIES:
        raise ValueError(f"Unknown translation strategy: {strategy}")
    if translator_only and strategy != "translator":
        raise ValueError("Translator-only mode supports only the translator strategy")

    def stage(name):
        if progress is not None:
            progress(name)

    stage("validate")
    data = source.read_bytes()
    validate_document(data, source.name)
    is_pdf = source.suffix.lower() == ".pdf"
    is_pptx = source.suffix.lower() == ".pptx"
    is_docx = source.suffix.lower() == ".docx"
    is_xlsx = source.suffix.lower() == ".xlsx"
    is_image = source.suffix.lower() in docintel_client.IMAGE_CONTENT_TYPES
    durations = {}
    stage("extract")
    started = perf_counter()
    if is_pdf:
        extracted = docintel_client.extract(data, job_id=job_id, positioned=True)
    elif is_image:
        extracted = docintel_client.extract(
            data, model="prebuilt-read", job_id=job_id,
            content_type=docintel_client.IMAGE_CONTENT_TYPES[source.suffix.lower()],
            positioned=True,
        )
    elif is_xlsx:
        extracted = xlsx_document.extract(data)
    elif is_docx:
        extracted = docx_document.extract(data)
    else:
        extracted = pptx_extract.extract(data, positioned=True)
    contains_images = extracted.get("containsImages", False)
    if is_pptx:
        local_translation.validate_pptx(data, allow_images=True)
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
    if strategy == "translator":
        if not is_pptx or contains_images:
            translated, translation = local_translation.translate_segments(
                extracted["segments"], source_language, target_language, job_id,
            )
        else:
            result, translation = local_translation.translate_pptx(
                data, glossary.tsv(prepared["terms"]), source_language, target_language, job_id,
            )
            translated = pptx_extract.extract(result)
    elif strategy == "direct_gpt":
        translated, translation = contextual_translation.translate(
            extracted["segments"], source_language, target_language, prepared["terms"], job_id,
            strategy=strategy,
        )
    else:
        baseline, translator_usage = local_translation.translate_segments(
            extracted["segments"], source_language, target_language, job_id,
        )
        refinement_input = [
            {**source_segment, "translatorOutput": translated_segment["text"]}
            for source_segment, translated_segment in zip(extracted["segments"], baseline["segments"], strict=True)
        ]
        translated, translation = contextual_translation.translate(
            refinement_input, source_language, target_language, prepared["terms"], job_id,
            strategy=strategy,
        )
        translation["translator"] = translator_usage
    layout_preserved = is_pptx and not contains_images and strategy == "translator"
    durations["translate"] = perf_counter() - started
    destination = output_dir / job_id
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "translated.txt").write_text(
        "Machine translation - not reviewed\n\n" + "\n\n".join(s["text"] for s in translated["segments"]),
        encoding="utf-8",
    )
    stage("reconstruct")
    started = perf_counter()
    reconstruction_warning = None
    if is_pdf:
        try:
            result = document_output.pdf(data, translated["segments"])
        except document_output.LayoutError as error:
            reconstruction_warning = (
                "Original PDF layout could not accommodate translated text. "
                "Output is a reflowed PDF; original artwork and layout are not retained."
            )
            logger.warning("jobId=%s stage=reconstruct event=pdfReflow reason=%s",
                           job_id, str(error))
            result = document_output.reflow_pdf(translated["segments"])
        filename = "translated.pdf"
    elif is_xlsx:
        result = xlsx_document.rebuild(data, translated["segments"])
        filename = "translated.xlsx"
    elif is_docx:
        result = docx_document.rebuild(data, translated["segments"])
        filename = "translated.docx"
    elif is_image:
        result = document_output.image(data, translated["segments"], source.suffix.lower())
        filename = "translated" + source.suffix.lower()
    elif not layout_preserved:
        result = document_output.pptx(data, translated["segments"], contains_images)
        filename = "translated.pptx"
    else:
        filename = "translated.pptx"
    durations["reconstruct"] = perf_counter() - started
    started = perf_counter()
    if translator_only or skip_review:
        report = {"jobId": job_id, "decision": "not_reviewed", "score": None, "issues": [],
                  "costSignals": {"tokens": sum(u["inputTokens"] + u["outputTokens"]
                                               for u in prepared.get("usage", []))},
                  "passA": [], "passB": [], "checkerAgreement": [],
                  "versions": [], "prepareUsage": prepared.get("usage", [])}
    else:
        stage("check")
        report = review_documents(
            {"jobId": job_id, "fileName": source.name, "targetLanguage": target_language}, prepared, translated,
        )
        durations["check"] = perf_counter() - started
    report.update({
        "jobId": job_id, "sourceFile": source.name, "sourceSha256": hashlib.sha256(data).hexdigest(),
        "sourceLanguage": source_language, "targetLanguage": target_language,
        "status": ("not_reviewed" if translator_only or skip_review else
                   "complete" if report["decision"] == "passed" else "needs_review"),
        "workflow": "translator_only" if translator_only else "assisted",
        "translation": {"strategy": strategy, **translation},
        "layoutPreserved": layout_preserved,
        "documentFidelity": {
            "evaluated": False,
            "status": "not_evaluated",
            "concerns": ["fonts", "logos_images", "tables", "layout_positioning", "page_slide_structure"],
        },
        "limitations": (["XLSX translates literal text cells, including hidden sheets. Formulas and cached "
                         "results, numbers, dates, sheet names, charts, images, comments and metadata remain "
                         "unchanged. Text-dependent formulas may behave differently after cell translation. "
                         "Cell styling is retained using the first rich-text run; column widths and row heights "
                         "are unchanged, so text fit requires manual review."] if is_xlsx else
                        ["DOCX retains document structure, tables, headers, footers and embedded images. "
                         "Translated paragraphs use the first text run's styling; mixed inline styling, "
                         "pagination and text fit require manual review. Text inside images is not translated. "
                         "Comments and document metadata are unchanged."] if is_docx else
                        ["PDF output retains original pages and artwork, with translated text overlaid in white "
                         "regions. Original text remains underneath and may be searchable or selectable. "
                         "Fonts, background colors, and text fit may differ; manual layout review is required.",
                         ("No glossary or quality review is performed." if translator_only else
                          "The glossary is checked by reviewers, not applied by Translator Text.")] if is_pdf else
                        ["Image output retains its dimensions and file type. Translated OCR text is drawn over "
                         "white regions; background artwork and typography in those regions are not preserved."] if is_image else
                        ["Image-bearing PPTX retains original slides and artwork with translated text overlaid "
                         "in white regions. Underlying source text remains; fonts and backgrounds may differ."] if contains_images else
                        ["Native-text PPTX retains slides and shapes. GPT replacement uses the first run's styling "
                         "per paragraph; mixed inline styling and text fit require manual review. "
                         "Charts are not supported."]),
    })
    if reconstruction_warning:
        report["limitations"][0] = reconstruction_warning
        report["documentFidelity"]["status"] = "layout_not_preserved"
    report["reconstruction"] = {
        "mode": "reflowed-pdf" if reconstruction_warning else "positioned",
        "warning": reconstruction_warning,
    }
    if skip_review and not translator_only:
        report["limitations"].append("Quality review is temporarily disabled. This translation is not approved.")
    report["costSignals"].update({
        **translation, "ocrPages": extracted.get("pageCount", 0), "durationsSeconds": durations,
    })
    text_output = "\n\n".join(s["text"] for s in translated["segments"]).encode("utf-8")
    if report["decision"] == "flagged":
        text_output = b"Machine translation - requires review\n\n" + text_output
        if is_xlsx:
            report["limitations"].append("Review warning is in the report and text sidecar, not in workbook cells.")
        elif is_docx:
            result = docx_document.rebuild(data, translated["segments"], flagged=True)
        elif not is_image:
            result = watermark(result, filename)
        else:
            report["limitations"].append("Review warning is in the report and text sidecar, not on the image.")
    stage("save")
    (destination / filename).write_bytes(result)
    (destination / "translated.txt").write_bytes(text_output)
    report["outputFile"] = filename
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
    parser.add_argument("--strategy", choices=sorted(contextual_translation.STRATEGIES), default="translator")
    args = parser.parse_args()
    job_id = uuid4().hex
    try:
        run_file(args.input, args.output_dir, args.source_language, args.target_language,
                 job_id=job_id, strategy=args.strategy)
    except Exception as error:
        print(f"Job failed ({type(error).__name__}); jobId={job_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
