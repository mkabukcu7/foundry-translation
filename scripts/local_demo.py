# SPDX-License-Identifier: MIT
"""Local-file translation and review; no Blob, Table, Queue, Functions host, or Scheduler."""

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from activities.check import review_documents
from activities.precheck import validate_document
from activities.route import watermark
from services import (
    contextual_translation,
    docintel_client,
    document_output,
    docx_document,
    glossary,
    local_translation,
    pptx_extract,
    telemetry,
    translation_engines,
    xlsx_document,
)
from services.config import ConfigurationError, integer

logger = logging.getLogger(__name__)


def _escalate_flagged_chunks(source_segments, target_segments, issues, terms, source_language,
                             target_language, job_id, glossary_mode):
    profile = translation_engines.config("gpt6_1_terra")
    seen, usages = set(), []
    skipped = 0
    for issue in issues:
        if issue.get("type") not in {"meaning_shift", "terminology"}:
            continue
        location = issue.get("location")
        key = (location, issue.get("source_excerpt"), issue.get("target_excerpt"))
        if key in seen:
            continue
        seen.add(key)
        if not location:
            skipped += 1
            continue
        source_excerpt = issue.get("source_excerpt", "").strip()
        target_excerpt = issue.get("target_excerpt", "").strip()
        if not source_excerpt or not target_excerpt:
            skipped += 1
            continue
        source = next((
            segment for segment in source_segments
            if segment.get("location") == location and source_excerpt.casefold() in segment["text"].casefold()
        ), None)
        target = next((
            segment for segment in target_segments
            if segment.get("location") == location and target_excerpt in segment["text"]
        ), None)
        if not source or not target:
            skipped += 1
            continue
        translated, usage = contextual_translation.translate(
            [{"text": source_excerpt, "location": location}],
            source_language, target_language, terms, job_id, strategy="direct_gpt",
            model=profile.deployment_name, reasoning_effort=profile.reasoning_effort,
            glossary_mode=glossary_mode,
        )
        translated_excerpt = translated["segments"][0]["text"]
        target["text"] = target["text"].replace(target_excerpt, translated_excerpt, 1)
        usages.append(usage)
        logger.info("jobId=%s event=hybridEscalation engine=%s type=%s inputTokens=%s outputTokens=%s",
                    job_id, profile.engine, issue["type"], usage["inputTokens"], usage["outputTokens"],
                    extra={"custom_dimensions": {
                        "jobId": job_id, "engine": profile.engine, "model": profile.model_name,
                        "deploymentType": profile.deployment_type,
                        "reasoningEffort": profile.reasoning_effort or "",
                        "issueType": issue["type"], "inputTokens": usage["inputTokens"],
                        "outputTokens": usage["outputTokens"],
                    }})
    return usages, len(seen), skipped, profile


def _rebuild_escalated_document(source_data, translated, extension, contains_images):
    if extension == ".pdf":
        return document_output.pdf(source_data, translated["segments"])
    if extension == ".docx":
        return docx_document.rebuild(source_data, translated["segments"])
    if extension == ".xlsx":
        return xlsx_document.rebuild(source_data, translated["segments"])
    if extension == ".pptx":
        return document_output.pptx(source_data, translated["segments"], contains_images, image_sidecar=True)
    if extension in docintel_client.IMAGE_CONTENT_TYPES:
        return document_output.image(source_data, translated["segments"], extension)
    raise ValueError(f"Hybrid escalation does not support {extension} reconstruction")


def _translation_text(segments):
    editable = []
    image_text = {}
    for segment in segments:
        if segment.get("role") == "image_text":
            image_text.setdefault(segment["page"], []).append(segment["text"])
        else:
            editable.append(segment["text"])
    if image_text:
        editable.append("Text from pictures (not inserted into PowerPoint)")
        for page, texts in sorted(image_text.items()):
            editable.append(f"Slide {page}\n" + "\n\n".join(texts))
    return "\n\n".join(editable)


def run_file(source: Path, output_dir: Path, source_language="it", target_language="en",
             *, job_id: str | None = None, progress: Callable[[str], None] | None = None,
             translator_only: bool = False, strategy: str = "translator", skip_review: bool = False,
             engine: str | None = None, reasoning_effort: str | None = None,
             glossary_mode: str | None = None, hybrid: bool | None = None) -> dict:
    job_id = job_id or uuid4().hex
    if strategy not in contextual_translation.STRATEGIES:
        raise ValueError(f"Unknown translation strategy: {strategy}")
    if translator_only and strategy != "translator":
        raise ValueError("Translator-only mode supports only the translator strategy")
    selected_engine = engine or {"translator": "translator", "direct_gpt": "gpt6_luna",
                                 "translator_refine": "gpt6_luna"}[strategy]
    engine_settings = translation_engines.config(selected_engine, reasoning_effort=reasoning_effort)
    if engine is not None:
        strategy = "translator" if selected_engine in {"translator", "doc_llm"} else "direct_gpt"
    use_pinned_agent = (
        engine is None and strategy == "direct_gpt"
        and bool(os.getenv("TRANSLATION_AGENT_NAME", "").strip())
    )
    model_override = None if use_pinned_agent else engine_settings.deployment_name
    glossary_mode = glossary_mode or os.getenv("GLOSSARY_MODE", "static").strip().lower()
    if glossary_mode not in {"none", "static", "retrieval"}:
        raise ValueError("Glossary mode must be none, static or retrieval")
    hybrid = hybrid if hybrid is not None else os.getenv("HYBRID_ROUTING", "false").strip().lower() == "true"
    if hybrid and (selected_engine not in {"translator", "gpt6_luna"} or translator_only or skip_review):
        raise ValueError("Hybrid routing requires Translator NMT or GPT-6 Luna with quality review enabled")
    review_job: dict[str, str | int] = {
        "jobId": job_id, "fileName": source.name, "targetLanguage": target_language,
    }
    if not (translator_only or skip_review):
        parallel_chunks = integer("LOCAL_REVIEW_PARALLEL_CHUNKS", 2)
        if parallel_chunks > 4:
            raise ConfigurationError("LOCAL_REVIEW_PARALLEL_CHUNKS must be between 1 and 4")
        review_job["parallelReviewChunks"] = parallel_chunks

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
        extracted = pptx_extract.extract(data, positioned=True, image_sidecar=True, job_id=job_id)
    contains_images = extracted.get("containsImages", False)
    if is_pptx:
        local_translation.validate_pptx(data, allow_images=True)
    durations["extract"] = perf_counter() - started
    if not any(s["text"].strip() for s in extracted["segments"]):
        raise ValueError(f"No extractable text; jobId={job_id}")
    started = perf_counter()
    prepared = {**extracted, "language": source_language, "terms": [], "usage": [],
                "standardGlossaryAvailable": source_language[:2] in glossary.LOCALES}
    if not translator_only and glossary_mode != "none":
        stage("prepare")
        glossary_path = os.getenv("GLOSSARY_PATH", "").strip()
        glossary_file = Path(glossary_path) if glossary_path else (
            Path(__file__).resolve().parents[1] / "src" / "glossaries" / "insurance-standard.tsv"
        )
        glossary_content = glossary_file.read_text(encoding="utf-8")
        source_text = "\n".join(segment["text"] for segment in extracted["segments"])
        prepared["terms"] = glossary.relevant_terms(
            glossary_content, source_language, target_language, source_text, glossary_mode,
        )
        durations["prepare"] = perf_counter() - started
    started = perf_counter()
    document_translated = False
    stage("translate")
    if selected_engine == "doc_llm":
        if not (is_docx or is_pptx):
            raise ValueError("DOC_LLM preview supports DOCX and PPTX inputs in this POC")
        result, translation = local_translation.translate_document(
            data, source.name, glossary.tsv(prepared["terms"]), source_language, target_language, job_id,
            engine=engine_settings,
        )
        translated = docx_document.extract(result) if is_docx else pptx_extract.extract(result, positioned=True)
        document_translated = True
        layout_preserved = True
    elif selected_engine == "translator":
        if not is_pptx or contains_images:
            if is_docx:
                result, translation = local_translation.translate_document(
                    data, source.name, glossary.tsv(prepared["terms"]), source_language, target_language, job_id,
                    engine=engine_settings,
                )
                translated = docx_document.extract(result)
                document_translated = True
                layout_preserved = True
            else:
                translated, translation = local_translation.translate_segments(
                    extracted["segments"], source_language, target_language, job_id,
                )
        else:
            result, translation = local_translation.translate_pptx(
                data, glossary.tsv(prepared["terms"]), source_language, target_language, job_id,
                api_version=engine_settings.api_version,
            )
            translated = pptx_extract.extract(result, positioned=True)
            document_translated = True
        layout_preserved = is_pptx and not contains_images or is_docx
    elif strategy == "direct_gpt":
        translated, translation = contextual_translation.translate(
            extracted["segments"], source_language, target_language, prepared["terms"], job_id,
            strategy=strategy, model=model_override,
            reasoning_effort=engine_settings.reasoning_effort,
            glossary_mode=glossary_mode,
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
            strategy=strategy, model=engine_settings.deployment_name,
            reasoning_effort=engine_settings.reasoning_effort,
            glossary_mode=glossary_mode,
        )
        translation["translator"] = translator_usage
    if selected_engine not in {"translator", "doc_llm"}:
        layout_preserved = False
    durations["translate"] = perf_counter() - started
    destination = output_dir / job_id
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "translated.txt").write_text(
        "Machine translation - not reviewed\n\n" + _translation_text(translated["segments"]),
        encoding="utf-8",
    )
    stage("reconstruct")
    started = perf_counter()
    reconstruction_warning = None
    if document_translated:
        filename = f"translated{source.suffix.lower()}"
    elif is_pdf:
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
        result = document_output.pptx(data, translated["segments"], contains_images, image_sidecar=True)
        filename = "translated.pptx"
    else:
        filename = "translated.pptx"
    durations["reconstruct"] = perf_counter() - started
    unreviewed_pptx = destination / "translated-unreviewed.pptx"
    if is_pptx and not (translator_only or skip_review):
        unreviewed_pptx.write_bytes(result)
    started = perf_counter()
    if translator_only or skip_review:
        report = {"jobId": job_id, "decision": "not_reviewed", "score": None, "issues": [],
                  "costSignals": {"tokens": sum(u["inputTokens"] + u["outputTokens"]
                                               for u in prepared.get("usage", []))},
                  "passA": [], "passB": [], "checkerAgreement": [],
                  "versions": [], "prepareUsage": prepared.get("usage", [])}
    else:
        stage("check")
        report = review_documents(review_job, prepared, translated)
        durations["check"] = perf_counter() - started
    hybrid_report = None
    if hybrid:
        escalation_usage, eligible, skipped, escalation_engine = _escalate_flagged_chunks(
            extracted["segments"], translated["segments"], report["issues"], prepared["terms"],
            source_language, target_language, job_id, glossary_mode,
        )
        escalation_costs = [
            translation_engines.estimate_cost(
                escalation_engine, input_tokens=usage["inputTokens"], output_tokens=usage["outputTokens"],
            )
            for usage in escalation_usage
        ]
        escalation_cost = (
            sum(escalation_costs) if escalation_usage and all(cost is not None for cost in escalation_costs)
            else 0 if not escalation_usage else None
        )
        hybrid_report = {
            "enabled": True,
            "eligibleChunks": eligible,
            "escalatedChunks": len(escalation_usage),
            "skippedChunks": skipped,
            "escalationRate": len(escalation_usage) / eligible if eligible else 0,
            "usage": escalation_usage,
            "estimatedCostUSD": escalation_cost if escalation_usage else 0,
            "model": escalation_engine.model_name,
            "deploymentName": escalation_engine.deployment_name,
        }
        if escalation_usage:
            initial_review = report
            try:
                result = _rebuild_escalated_document(data, translated, source.suffix.lower(), contains_images)
            except document_output.LayoutError as error:
                if not is_pdf:
                    raise
                reconstruction_warning = (
                    "Hybrid translation did not fit the original PDF layout. "
                    "Output is a reflowed PDF; original artwork and layout are not retained."
                )
                logger.warning("jobId=%s stage=reconstruct event=pdfReflow reason=%s", job_id, str(error))
                result = document_output.reflow_pdf(translated["segments"])
            hybrid_review_started = perf_counter()
            stage("check")
            report = review_documents(review_job, prepared, translated)
            durations["hybridCheck"] = perf_counter() - hybrid_review_started
            report["initialReview"] = {
                "score": initial_review["score"],
                "issues": initial_review["issues"],
                "versions": initial_review.get("versions", []),
                "costSignals": initial_review.get("costSignals", {}),
            }
        report["hybridRouting"] = hybrid_report
    translation["inputCharacters"] = sum(len(segment["text"]) for segment in extracted["segments"])
    translation["outputCharacters"] = sum(len(segment["text"]) for segment in translated["segments"])
    translation_cost = (
        None if translation.get("agent") else translation_engines.estimate_cost(
            engine_settings,
            input_tokens=translation.get("inputTokens", 0),
            output_tokens=translation.get("outputTokens", 0),
            characters=(
                translation.get("charactersCharged")
                if translation.get("charactersCharged") is not None
                else translation["inputCharacters"]
            ),
        )
    )
    translation["estimatedCostUSD"] = translation_cost
    if hybrid_report is not None:
        translation["escalationUsage"] = hybrid_report["usage"]
        translation["escalationEstimatedCostUSD"] = hybrid_report["estimatedCostUSD"]
        if translation_cost is not None and hybrid_report["estimatedCostUSD"] is not None:
            translation["estimatedCostUSD"] = translation_cost + hybrid_report["estimatedCostUSD"]
    reviewer_usage = [
        *report.get("prepareUsage", []),
        *report.get("versions", []),
        *(report.get("initialReview", {}).get("versions", [])),
    ]
    reviewer_costs = [
        translation_engines.estimate_role_cost(usage.get("agent", ""), usage)
        for usage in reviewer_usage
    ]
    review_cost = (
        sum(reviewer_costs) if reviewer_costs and all(cost is not None for cost in reviewer_costs)
        else 0 if not reviewer_costs else None
    )
    total_cost = (
        translation["estimatedCostUSD"] + review_cost
        if translation["estimatedCostUSD"] is not None and review_cost is not None else None
    )
    engine_details = translation_engines.metadata(engine_settings)
    if translation.get("agent"):
        engine_details.update(
            engine="foundry_agent",
            model=translation["model"],
            deploymentName=None,
            deploymentType=None,
            modelVersion=None,
            maturity=None,
            reasoningEffort=None,
            agent=translation["agent"],
            agentVersion=translation.get("version"),
        )
    report.update({
        "jobId": job_id, "sourceFile": source.name, "sourceSha256": hashlib.sha256(data).hexdigest(),
        "sourceLanguage": source_language, "targetLanguage": target_language,
        "status": ("not_reviewed" if translator_only or skip_review else
                   "complete" if report["decision"] == "passed" else "needs_review"),
        "workflow": "translator_only" if translator_only else "assisted",
        "translation": {
            "strategy": strategy, **translation,
            "engineDetails": engine_details,
        },
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
                        ["Image-bearing PPTX retains original slides and artwork, and replaces editable text "
                         "in its original shapes and table cells. Translated picture text is in the TXT sidecar only. "
                         "Mixed inline styling and text fit require manual review."] if contains_images else
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
        "reviewEstimatedCostUSD": review_cost,
        "estimatedCostUSD": total_cost,
        "tokens": report["costSignals"].get("tokens", 0)
        + translation.get("inputTokens", 0) + translation.get("outputTokens", 0)
        + sum(
            usage.get("inputTokens", 0) + usage.get("outputTokens", 0)
            for usage in (hybrid_report or {}).get("usage", [])
        )
        + sum(
            usage.get("inputTokens", 0) + usage.get("outputTokens", 0)
            for usage in report.get("initialReview", {}).get("versions", [])
        ),
    })
    logger.info(
        "jobId=%s event=translationMetrics engine=%s maturity=%s reasoningEffort=%s "
        "inputTokens=%s outputTokens=%s reasoningTokens=%s charactersCharged=%s "
        "durationSeconds=%.3f estimatedCostUSD=%s",
        job_id, engine_settings.engine, engine_settings.maturity, engine_settings.reasoning_effort,
        translation.get("inputTokens", 0), translation.get("outputTokens", 0),
        translation.get("reasoningTokens", 0), translation.get("charactersCharged"),
        durations.get("translate", 0), translation.get("estimatedCostUSD"),
        extra={"custom_dimensions": {
            "jobId": job_id,
            "engine": engine_settings.engine,
            "model": engine_settings.model_name,
            "deploymentType": engine_settings.deployment_type,
            "modelVersion": engine_settings.model_version or "",
            "apiVersion": engine_settings.api_version,
            "maturity": engine_settings.maturity,
            "reasoningEffort": engine_settings.reasoning_effort or "",
            "inputTokens": translation.get("inputTokens", 0),
            "outputTokens": translation.get("outputTokens", 0),
            "reasoningTokens": translation.get("reasoningTokens", 0),
            "durationSeconds": durations.get("translate", 0),
            "estimatedCostUSD": translation.get("estimatedCostUSD"),
        }},
    )
    telemetry.record_translation_metrics(
        job_id, engine_settings, translation, duration_by_step=durations,
        estimated_cost=translation.get("estimatedCostUSD"),
    )
    for usage in reviewer_usage:
        role = usage.get("agent", "")
        deployment_setting = {
            "translation-checker": "CHECKER_DEPLOYMENT_TYPE",
            "translation-checker-secondary": "MISTRAL_CHECKER_DEPLOYMENT_TYPE",
            "term-guard": "TERM_GUARD_DEPLOYMENT_TYPE",
        }.get(role, "")
        deployment_type = (
            os.getenv(deployment_setting, "").strip()
            or os.getenv("GPT6_LUNA_DEPLOYMENT_TYPE", "Global").strip()
        ) if deployment_setting else "Global"
        telemetry.record_role_metrics(
            job_id, role, usage, deployment_type=deployment_type,
            estimated_cost=translation_engines.estimate_role_cost(role, usage),
        )
    if hybrid_report is not None:
        escalation_profile = translation_engines.config("gpt6_1_terra")
        for usage in hybrid_report["usage"]:
            telemetry.record_translation_metrics(
                job_id, escalation_profile, usage,
                duration_by_step={"escalation": usage["durationSeconds"]},
                estimated_cost=translation_engines.estimate_cost(
                    escalation_profile, input_tokens=usage["inputTokens"], output_tokens=usage["outputTokens"],
                ),
            )
    text_output = _translation_text(translated["segments"]).encode("utf-8")
    if report["decision"] == "flagged":
        text_output = b"Machine translation - requires review\n\n" + text_output
        if is_xlsx or is_pptx:
            report["limitations"].append("Review warning is in the report and text sidecar, not in the translated document.")
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
    unreviewed_pptx.unlink(missing_ok=True)
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
    parser.add_argument("--engine", choices=translation_engines.ENGINES)
    parser.add_argument("--reasoning-effort", choices=translation_engines.REASONING_EFFORTS)
    parser.add_argument("--glossary-mode", choices=("none", "static", "retrieval"))
    parser.add_argument("--hybrid", action="store_true")
    args = parser.parse_args()
    job_id = uuid4().hex
    try:
        run_file(args.input, args.output_dir, args.source_language, args.target_language,
                 job_id=job_id, strategy=args.strategy, engine=args.engine,
                 reasoning_effort=args.reasoning_effort, glossary_mode=args.glossary_mode, hybrid=args.hybrid)
    except Exception as error:
        print(f"Job failed ({type(error).__name__}); jobId={job_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
