# SPDX-License-Identifier: MIT
"""Live translation comparison. Run PYTHONPATH=src:. python eval/run_eval.py.

The three local translation strategies, customer baseline and production pipeline use the
same reviewer and seeded-value oracle. Baseline latency and historical usage are unknown,
never reported as zero. Missing baselines are explicit.
"""

import argparse
import csv
import json
import time
import uuid
from collections import Counter
from pathlib import Path

import requests

from activities.check import review_documents
from scripts.translator_spike import translate_file
from services import contextual_translation, extraction, glossary, local_translation, normalize, storage
from services.config import credential, required

ROOT = Path(__file__).resolve().parents[1]
ISSUE_TYPES = ["omission", "addition", "meaning_shift", "terminology", "number", "tone", "formatting"]
COLUMNS = [
    "document", "arm", "status", "score", "pipelineScore", "passAMissesVsExpected",
    "latencySeconds", "characters", "images", "tokens", "model", "inputTokens",
    "outputTokens", "modelLatencySeconds", "translatorCharactersSubmitted",
    "translatorCharactersCharged", "evaluationTokens", *ISSUE_TYPES,
]


def api_request(method, url, **kwargs):
    token = credential().get_token(f"api://{required('API_CLIENT_ID')}/.default").token
    response = requests.request(method, url, headers={"Authorization": f"Bearer {token}"}, timeout=90, **kwargs)
    response.raise_for_status()
    return response


def pipeline(path, language, timeout=3600):
    base = required("API_BASE_URL").rstrip("/")
    with path.open("rb") as document:
        accepted = api_request("POST", base + "/jobs", files={"file": (path.name, document)},
                               data={"targetLanguage": "en", "sourceLanguage": language}).json()
    deadline = time.monotonic() + timeout
    status_url = base + "/jobs/" + accepted["jobId"]
    while time.monotonic() < deadline:
        status = api_request("GET", status_url).json()
        if status["status"] == "failed":
            raise RuntimeError(f"Pipeline failed jobId={accepted['jobId']}")
        if status["status"] in {"complete", "needs_review"}:
            return api_request("GET", status_url + "/result").content, status
        time.sleep(5)
    raise TimeoutError("Pipeline evaluation timed out")


def oracle_misses(meta, translated):
    values = [value for key in ("policy_numbers", "claim_numbers", "amounts", "dates", "names", "addresses")
              for value in meta[key]]
    segments = [{"text": "\n".join(values), "location": "seeded-values"}]
    terms = [{"term": value, "type": "other", "action": "keep", "target": None}
             for key in ("names", "addresses") for value in meta[key]]
    findings = normalize.compare(segments, translated["segments"], terms,
                                 glossary.LOCALES[meta["sourceLanguage"]], "en_US")
    return sum(f["match"] == "unmatched" and f["side"] == "source" for f in findings)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results.csv")
    parser.add_argument("--baseline", type=Path, default=ROOT / "eval/baseline")
    args = parser.parse_args()
    expected = json.loads((ROOT / "samples/expected.json").read_text())
    standard = (ROOT / "src/glossaries/insurance-standard.tsv").read_text()
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, COLUMNS)
        writer.writeheader()
        for filename, meta in expected.items():
            path = ROOT / "samples/out" / filename
            extracted = extraction.extract(path.read_bytes(), filename)
            source_text = "\n".join(s["text"] for s in extracted["segments"])
            oracle_terms = [{"term": v, "type": "other", "action": "keep", "target": None}
                            for key in ("policy_numbers", "claim_numbers", "names", "addresses")
                            for v in meta[key]]
            terms = glossary.validate_terms(
                oracle_terms + glossary.standard_terms(standard, meta["sourceLanguage"], "en", source_text),
                source_text,
            )
            prepared = {**extracted, "language": meta["sourceLanguage"], "terms": terms, "usage": []}
            for arm in ("azure_translator", "direct_gpt", "translator_refine",
                        "customer_baseline", "production_pipeline"):
                row = {"document": filename, "arm": arm}
                started = time.monotonic()
                signals = {}
                model_usage = {}
                job_id = uuid.uuid4().hex
                if arm == "azure_translator":
                    result, signals = translate_file(path, meta["sourceLanguage"], use_glossary=False)
                    translated = extraction.extract(result, filename)
                elif arm == "direct_gpt":
                    translated, model_usage = contextual_translation.translate(
                        prepared["segments"], meta["sourceLanguage"], "en", terms, job_id,
                        strategy="direct_gpt",
                    )
                elif arm == "translator_refine":
                    baseline, translator_usage = local_translation.translate_segments(
                        prepared["segments"], meta["sourceLanguage"], "en", job_id,
                    )
                    refinement_input = [
                        {**source, "translatorOutput": target["text"]}
                        for source, target in zip(prepared["segments"], baseline["segments"], strict=True)
                    ]
                    translated, model_usage = contextual_translation.translate(
                        refinement_input, meta["sourceLanguage"], "en", terms, job_id,
                        strategy="translator_refine",
                    )
                    model_usage["translator"] = translator_usage
                    signals = {
                        "characters": translator_usage["charactersCharged"]
                        if translator_usage["charactersCharged"] is not None
                        else translator_usage["charactersSubmitted"],
                        "images": translator_usage["imagesTranslated"],
                    }
                elif arm == "customer_baseline":
                    baseline = args.baseline / filename
                    if not baseline.exists():
                        writer.writerow({**row, "status": "baseline_missing"})
                        stream.flush()
                        continue
                    result = baseline.read_bytes()
                else:
                    result, status = pipeline(path, meta["sourceLanguage"])
                    if status["status"] == "needs_review":
                        # Judge the same unmodified text as the pipeline, not its required review banner.
                        _, result = storage.target({"jobId": status["jobId"], "fileName": filename})
                    signals = {"characters": status["chars"], "images": status["images"],
                               "tokens": status["tokens"]}
                    row["pipelineScore"] = status["score"]
                latency = time.monotonic() - started if arm != "customer_baseline" else None
                if arm in {"customer_baseline", "production_pipeline"}:
                    translated = extraction.extract(result, filename)
                report = review_documents({
                    "jobId": uuid.uuid4().hex, "fileName": filename, "targetLanguage": "en",
                }, prepared, translated)
                issue_counts = Counter(issue["type"] for issue in report["issues"])
                writer.writerow({
                    **row, "status": "evaluated", "score": report["score"],
                    "passAMissesVsExpected": oracle_misses(meta, translated),
                    "latencySeconds": latency, "characters": signals.get("characters"),
                    "images": signals.get("images"), "tokens": signals.get("tokens"),
                    "model": model_usage.get("model"),
                    "inputTokens": model_usage.get("inputTokens"),
                    "outputTokens": model_usage.get("outputTokens"),
                    "modelLatencySeconds": model_usage.get("durationSeconds"),
                    "translatorCharactersSubmitted": model_usage.get("translator", {}).get(
                        "charactersSubmitted",
                    ),
                    "translatorCharactersCharged": model_usage.get("translator", {}).get(
                        "charactersCharged",
                    ),
                    "evaluationTokens": report["costSignals"]["tokens"],
                    **{kind: issue_counts[kind] for kind in ISSUE_TYPES},
                })
                stream.flush()


if __name__ == "__main__":
    main()
