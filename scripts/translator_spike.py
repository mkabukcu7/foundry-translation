# SPDX-License-Identifier: MIT
"""Live phase 1a spike, sharing the production Translator adapter.

Run with PYTHONPATH=src after generating samples. No document content is logged.
"""

import argparse
import json
import time
import uuid
from pathlib import Path

from services import glossary, storage
from services.translator_client import TERMINAL, Translator

ROOT = Path(__file__).resolve().parents[1]


def translate_file(path, language, target="en", use_glossary=True, timeout=1800):
    job_id = uuid.uuid4().hex
    job = {"jobId": job_id, "fileName": path.name, "sourceLanguage": language, "targetLanguage": target}
    for container in ("source", "glossaries", "reports", f"target-{job_id}"):
        storage.ensure_container(container)
    source_path = f"{job_id}/{path.name}"
    storage.put("source", source_path, path.read_bytes())
    glossary_url = None
    if use_glossary:
        standard = (ROOT / "src/glossaries/aon-standard.tsv").read_text()
        # The spike tests every approved row; the production prepare activity filters to source text.
        rows = standard.splitlines()
        header = rows[0].split("\t")
        si, ti = header.index(language), header.index(target)
        terms = [{"term": r.split("\t")[si], "type": "insurance_term", "action": "map",
                  "target": r.split("\t")[ti]} for r in rows[1:]]
        storage.put("glossaries", f"{job_id}.tsv", glossary.tsv(terms), "text/tab-separated-values")
        glossary_url = storage.url("glossaries", f"{job_id}.tsv")
    adapter = Translator()
    operation = adapter.submit(job, storage.url("source", source_path), storage.url(f"target-{job_id}", path.name),
                               glossary_url)
    started, delay = time.monotonic(), 2
    while time.monotonic() - started < timeout:
        state = adapter.poll(operation, job_id)
        print(f"jobId={job_id} status={state['status']}", flush=True)
        if state["status"] in TERMINAL:
            documents = adapter.documents(operation, job_id)
            if state["status"] != "Succeeded" or not documents or any(d["status"] != "Succeeded" for d in documents):
                storage.put_json(job_id, "translation", {"batch": state, "documents": documents})
                raise RuntimeError(f"Translation failed; inspect protected reports for jobId={job_id}")
            target_name, result = storage.target({**job, "sourcePath": source_path})
            return result, {
                "jobId": job_id, "status": state["status"], "resultName": target_name,
                "documents": documents, "latencySeconds": time.monotonic() - started,
                "characters": sum(d.get("characterCharged", 0) for d in documents),
                "images": sum(d.get("imageCharged", 0) for d in documents),
            }
        time.sleep(min(delay, max(0, timeout - (time.monotonic() - started))))
        delay = min(delay * 2, 30)
    raise TimeoutError(f"Translation exceeded {timeout}s; jobId={job_id}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["en", "it", "es"], default="en")
    parser.add_argument("--source", choices=["en", "it", "es"], help="Source language for --file")
    parser.add_argument("--file", type=Path, help="Custom document, e.g. an English reverse-direction input")
    parser.add_argument("--no-glossary", action="store_true")
    args = parser.parse_args()
    if args.file and not args.source:
        parser.error("--file requires --source")
    expected = json.loads((ROOT / "samples/expected.json").read_text())
    files = [(args.file, args.source)] if args.file else [
        (ROOT / "samples/out" / name, meta["sourceLanguage"]) for name, meta in expected.items()
    ]
    out = ROOT / "samples/results"
    out.mkdir(exist_ok=True)
    for path, source_language in files:
        result, signals = translate_file(path, source_language, args.target, not args.no_glossary)
        (out / f"{path.stem}.{args.target}{path.suffix}").write_bytes(result)
        (out / f"{path.stem}.status.json").write_text(json.dumps(signals, indent=2))


if __name__ == "__main__":
    main()
