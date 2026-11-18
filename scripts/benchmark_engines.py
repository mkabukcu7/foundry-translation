# SPDX-License-Identifier: MIT
"""Run a configurable translation-engine matrix on synthetic files from samples/out."""

import argparse
import csv
import json
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import requests
from azure.core.exceptions import AzureError
from openai import APIError
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfReader

from scripts.local_demo import run_file
from services import docx_document
from services.config import ConfigurationError, ExternalServiceError, id_patterns
from services.pptx_extract import all_shapes
from services.translation_engines import ENGINES, REASONING_EFFORTS

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = {".pdf", ".pptx", ".docx", ".xlsx", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
PATTERNS = {
    "numbers": re.compile(r"(?<!\w)[+-]?\d+(?:[.,]\d+)*(?!\w)"),
    "dates": re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b"),
    "currency": re.compile(r"(?:€|EUR|USD|GBP)\s*[\d.,]+|[\d.,]+\s*(?:€|EUR|USD|GBP)", re.I),
}
BASE_COLUMNS = [
    "document", "engine", "maturity", "model", "modelVersion", "apiVersion", "deploymentType",
    "reasoningEffort", "glossaryMode", "hybrid", "status", "wallSeconds", "secondsPerPage",
    "inputTokens", "outputTokens", "reasoningTokens", "inputCharacters", "outputCharacters",
    "charactersCharged", "estimatedCostUSD", "stepDurationsSeconds",
    "terminologyScore", "reviewerIssues", "reviewerMeaningChanges", "escalationRate",
    "sourceStructure", "outputStructure",
    "structureMatches", "numbersExact", "datesExact", "idsExact", "currencyExact", "errorType",
]


def exact_values(source: str, target: str) -> dict:
    patterns = {**PATTERNS, "ids": [re.compile(pattern) for pattern in id_patterns()]}
    results = {}
    for kind, pattern in patterns.items():
        source_values = [
            match.group(0) for regex in (pattern if isinstance(pattern, list) else [pattern])
            for match in regex.finditer(source)
        ]
        target_values = [
            match.group(0) for regex in (pattern if isinstance(pattern, list) else [pattern])
            for match in regex.finditer(target)
        ]
        source_counts, target_counts = Counter(source_values), Counter(target_values)
        matched = sum(min(count, target_counts[value]) for value, count in source_counts.items())
        results[kind] = {
            "sourceCount": sum(source_counts.values()),
            "matchedCount": matched,
            "exact": matched == sum(source_counts.values()),
        }
    return results


def terminology_score(terms: list[dict], translated_text: str) -> float | None:
    if not terms:
        return None
    correct = sum(
        1 for term in terms
        if (term["term"] if term["action"] == "keep" else term["target"]).casefold()
        in translated_text.casefold()
    )
    return correct / len(terms) * 100


def document_structure(path: Path) -> dict:
    suffix = path.suffix.lower()
    result = {"pages": None, "slides": None, "sheets": None, "paragraphs": None, "tables": None, "images": None}
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        result.update(
            pages=len(reader.pages),
            paragraphs=sum(
                sum(bool(line.strip()) for line in (page.extract_text() or "").splitlines())
                for page in reader.pages
            ),
            images=sum(len(page.images) for page in reader.pages),
        )
    elif suffix == ".pptx":
        presentation = Presentation(path)
        shapes = [shape for slide in presentation.slides for shape in all_shapes(slide.shapes)]
        result.update(
            slides=len(presentation.slides),
            paragraphs=sum(
                len(frame.paragraphs)
                for shape in shapes
                for frame in (
                    ([shape.text_frame] if shape.has_text_frame else [])
                    + ([cell.text_frame for row in shape.table.rows for cell in row.cells]
                       if shape.has_table else [])
                )
            ),
            tables=sum(shape.has_table for shape in shapes),
            images=sum(shape.shape_type == MSO_SHAPE_TYPE.PICTURE for shape in shapes),
        )
    elif suffix == ".docx":
        with ZipFile(path) as archive:
            xml_parts = [name for name in archive.namelist()
                         if name.startswith("word/") and name.endswith(".xml")]
            roots = [docx_document.parse(archive.read(name)) for name in xml_parts]
            result.update(
                paragraphs=sum(len(root.xpath(".//w:p", namespaces=docx_document.NS)) for root in roots),
                tables=sum(len(root.xpath(".//w:tbl", namespaces=docx_document.NS)) for root in roots),
                images=sum(name.startswith("word/media/") for name in archive.namelist()),
            )
    elif suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(path, data_only=False)
        result.update(
            sheets=len(workbook.worksheets),
            paragraphs=sum(
                isinstance(cell.value, str) and not cell.value.startswith("=")
                for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row
            ),
            tables=sum(len(sheet.tables) for sheet in workbook.worksheets),
            images=sum(len(sheet._images) for sheet in workbook.worksheets),
        )
    elif suffix in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}:
        from PIL import Image

        with Image.open(path) as image:
            result.update(pages=getattr(image, "n_frames", 1), images=1)
    return result


def structure_matches(source: dict, output: dict) -> bool | None:
    comparable = [(key, source[key], output[key]) for key in source
                  if source[key] is not None and output[key] is not None]
    return all(left == right for _, left, right in comparable) if comparable else None


def run_case(path, output_dir, source_language, target_language, engine, glossary_mode,
             reasoning_effort, hybrid, skip_review):
    row = {
        "document": path.name, "engine": engine, "glossaryMode": glossary_mode,
        "reasoningEffort": reasoning_effort, "hybrid": hybrid, "status": "failed",
    }
    if engine == "doc_llm" and path.suffix.lower() not in {".docx", ".pptx"}:
        row["status"] = "skipped_unsupported_format"
        return row
    if hybrid and engine not in {"translator", "gpt6_luna"}:
        row["status"] = "skipped_hybrid_engine"
        return row
    job_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        report = run_file(
            path, output_dir, source_language, target_language, job_id=job_id, engine=engine,
            reasoning_effort=reasoning_effort, glossary_mode=glossary_mode, hybrid=hybrid,
            skip_review=skip_review,
        )
    except (ConfigurationError, ExternalServiceError, APIError, AzureError,
            requests.RequestException, OSError, ValueError) as error:
        row["wallSeconds"] = time.perf_counter() - started
        row["errorType"] = type(error).__name__
        print(f"jobId={job_id} benchmarkCaseFailed errorType={type(error).__name__}", file=sys.stderr)
        return row
    wall_seconds = time.perf_counter() - started
    report_dir = output_dir / job_id
    preview = json.loads((report_dir / "showcase.json").read_text(encoding="utf-8"))
    output = report_dir / report["outputFile"]
    source_structure, output_structure = document_structure(path), document_structure(output)
    fidelity = exact_values(preview["sourceText"], preview["translatedText"])
    translation = report["translation"]
    details = translation["engineDetails"]
    pages = source_structure["pages"] or source_structure["slides"] or 1
    routing = report.get("hybridRouting", {})
    row.update({
        "status": report["status"],
        "maturity": details["maturity"],
        "model": details["model"],
        "modelVersion": details["modelVersion"],
        "apiVersion": details["apiVersion"],
        "deploymentType": details["deploymentType"],
        "wallSeconds": wall_seconds,
        "secondsPerPage": wall_seconds / pages,
        "inputTokens": translation.get("inputTokens"),
        "outputTokens": translation.get("outputTokens"),
        "reasoningTokens": translation.get("reasoningTokens"),
        "inputCharacters": len(preview["sourceText"]),
        "outputCharacters": len(preview["translatedText"]),
        "charactersCharged": translation.get("charactersCharged"),
        "estimatedCostUSD": report.get("costSignals", {}).get("estimatedCostUSD"),
        "stepDurationsSeconds": json.dumps(report.get("costSignals", {}).get("durationsSeconds", {}), sort_keys=True),
        "terminologyScore": terminology_score(preview["terms"], preview["translatedText"]),
        "reviewerIssues": len(report.get("issues", [])) if report["status"] != "not_reviewed" else None,
        "reviewerMeaningChanges": (
            sum(issue["type"] == "meaning_shift" for issue in report.get("issues", []))
            if report["status"] != "not_reviewed" else None
        ),
        "escalationRate": routing.get("escalationRate"),
        "sourceStructure": json.dumps(source_structure, sort_keys=True),
        "outputStructure": json.dumps(output_structure, sort_keys=True),
        "structureMatches": structure_matches(source_structure, output_structure),
        "numbersExact": fidelity["numbers"]["exact"],
        "datesExact": fidelity["dates"]["exact"],
        "idsExact": fidelity["ids"]["exact"],
        "currencyExact": fidelity["currency"]["exact"],
    })
    return row


def write_summary(rows, path):
    engines = ("gpt6_luna", "gpt6_1_terra", "gpt5_6_luna")
    lines = [
        "# Translation engine benchmark",
        "",
        "Rates are estimates from configured environment values; missing rates remain unknown. "
        "Results are POC measurements, not certification.",
        "",
        "| Engine | Status | Mean seconds/document | Mean terminology score | Mean estimated cost USD |",
        "|---|---|---:|---:|---:|",
    ]
    for engine in engines:
        valid = [row for row in rows if row["engine"] == engine and row["status"] not in {
            "failed", "skipped_unsupported_format", "skipped_hybrid_engine",
        }]
        maturity = next((row.get("maturity") for row in valid if row.get("maturity")), "not run")
        values = (
            statistics.mean([row["wallSeconds"] for row in valid if row.get("wallSeconds") is not None])
            if any(row.get("wallSeconds") is not None for row in valid) else None,
            statistics.mean([row["terminologyScore"] for row in valid if row.get("terminologyScore") is not None])
            if any(row.get("terminologyScore") is not None for row in valid) else None,
            statistics.mean([row["estimatedCostUSD"] for row in valid if row.get("estimatedCostUSD") is not None])
            if any(row.get("estimatedCostUSD") is not None for row in valid) else None,
        )
        formatted = [f"{value:.2f}" if value is not None else "unknown" for value in values]
        lines.append(f"| {engine} | {maturity} | {formatted[0]} | {formatted[1]} | {formatted[2]} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, default=ROOT / "samples" / "out")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "samples" / "results" / "benchmark")
    parser.add_argument("--source-language", default="it")
    parser.add_argument("--target-language", default="en")
    parser.add_argument("--engines", nargs="+", choices=ENGINES, default=ENGINES)
    parser.add_argument("--glossary-modes", nargs="+", choices=("none", "static", "retrieval"),
                        default=("none", "static", "retrieval"))
    parser.add_argument("--reasoning-efforts", nargs="+", choices=REASONING_EFFORTS,
                        default=REASONING_EFFORTS)
    parser.add_argument("--hybrid", action="store_true")
    parser.add_argument("--skip-review", action="store_true",
                        help="Skip billable reviewers; reviewer issue metrics will be unavailable.")
    parser.add_argument("--dry-run", action="store_true", help="Print matrix size without making Azure calls.")
    args = parser.parse_args()
    files = sorted(path for path in args.samples.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED)
    if not files:
        parser.error(f"No supported sample documents found in {args.samples}")
    cases = len(files) * len(args.engines) * len(args.glossary_modes) * len(args.reasoning_efforts)
    if args.dry_run:
        print(f"Would run {cases} cases across {len(files)} sample files.")
        return 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "benchmark.csv"
    rows = []
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=BASE_COLUMNS)
        writer.writeheader()
        for path in files:
            for engine in args.engines:
                for glossary_mode in args.glossary_modes:
                    for effort in args.reasoning_efforts:
                        row = run_case(
                            path, args.output_dir, args.source_language, args.target_language,
                            engine, glossary_mode, effort, args.hybrid, args.skip_review,
                        )
                        rows.append(row)
                        writer.writerow(row)
                        stream.flush()
    write_summary(rows, args.output_dir / "benchmark.md")
    print(f"Benchmark finished: {csv_path} ({len(rows)} rows)")
    return 1 if any(row["status"] == "failed" for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
