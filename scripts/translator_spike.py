# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Phase 1a spike: translate the sample docs with Azure Translator batch API.

Auth is Microsoft Entra ID only (DefaultAzureCredential): no keys, no SAS.
Config comes from environment variables (see .env.sample).

Usage:
    python samples/generate_samples.py
    python scripts/translator_spike.py [--target en] [--no-glossary]

Items marked TODO(verify) were taken from a secondary summary of the
2026-03-01 docs and MUST be confirmed on Microsoft Learn before relying on them:
  https://learn.microsoft.com/azure/ai-services/translator/document-translation/latest/rest-api/translate-asynchronous
"""
import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

API_VERSION = "2026-03-01"
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "out"
OUT = ROOT / "samples" / "results"
TERMINAL = {"Succeeded", "Failed", "Cancelled", "ValidationFailed"}

log = logging.getLogger("spike")


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name} (see .env.sample)")
    return value


def start_batch(endpoint, token, job_id, blob_url, filename, target_lang, source_lang, glossary_url):
    is_pptx = filename.lower().endswith(".pptx")
    target = {"targetUrl": f"{blob_url}/target-{job_id}", "language": target_lang}
    if glossary_url:
        target["glossaries"] = [{"glossaryUrl": glossary_url, "format": "TSV"}]
    source = {
        "sourceUrl": f"{blob_url}/source",
        "filter": {"prefix": f"{job_id}/{filename}"},
    }
    if source_lang:  # omitted => auto-detect
        source["language"] = source_lang
    item = {"storageType": "Folder", "source": source, "targets": [target]}
    # TODO(verify): option names and placement of the two flags below.
    if is_pptx:
        item["translateTextWithinImage"] = True
    else:
        item["pdfDocumentIntelligence"] = {"enabled": True}
    body = {"inputs": [item]}
    url = f"{endpoint}/translator/document/batches?api-version={API_VERSION}"
    # TODO(verify): bearer auth against the custom-subdomain endpoint, with the
    # Translator system-assigned managed identity reading/writing blobs via RBAC.
    resp = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if resp.status_code >= 300:
        log.error("jobId=%s start failed status=%s body=%s", job_id, resp.status_code, resp.text)
        resp.raise_for_status()
    return resp.headers["Operation-Location"]


def poll(op_url, token, job_id, timeout_s=1800):
    delay, deadline = 2, time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(op_url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        r.raise_for_status()
        status = r.json()
        log.info("jobId=%s status=%s summary=%s", job_id, status.get("status"), status.get("summary"))
        if status.get("status") in TERMINAL:
            return status
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise TimeoutError(f"jobId={job_id} did not finish in {timeout_s}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="en")
    ap.add_argument("--no-glossary", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    endpoint = env("TRANSLATOR_ENDPOINT").rstrip("/")  # custom-subdomain endpoint
    blob_url = env("STORAGE_BLOB_ENDPOINT").rstrip("/")  # https://<acct>.blob.core.windows.net
    expected = json.loads((ROOT / "samples" / "expected.json").read_text(encoding="utf-8"))

    cred = DefaultAzureCredential()
    blobs = BlobServiceClient(blob_url, credential=cred)
    token = cred.get_token("https://cognitiveservices.azure.com/.default").token

    for name in ("source", "glossaries"):
        try:
            blobs.create_container(name)
        except ResourceExistsError:
            pass

    glossary_url = None
    if not args.no_glossary:
        gl = ROOT / "samples" / "aon-standard.tsv"
        blobs.get_blob_client("glossaries", gl.name).upload_blob(gl.read_bytes(), overwrite=True)
        glossary_url = f"{blob_url}/glossaries/{gl.name}"

    OUT.mkdir(parents=True, exist_ok=True)
    for filename, meta in expected.items():
        path = SAMPLES / filename
        if not path.exists():
            sys.exit(f"{path} missing; run samples/generate_samples.py first")
        job_id = uuid.uuid4().hex[:12]
        log.info("jobId=%s file=%s bytes=%d", job_id, filename, path.stat().st_size)
        blobs.get_blob_client("source", f"{job_id}/{filename}").upload_blob(path.read_bytes())
        try:
            blobs.create_container(f"target-{job_id}")
        except ResourceExistsError:
            pass

        started = time.time()
        op = start_batch(endpoint, token, job_id, blob_url, filename, args.target,
                         meta.get("sourceLanguage"), glossary_url)
        status = poll(op, token, job_id)
        elapsed = time.time() - started

        # Cost signals: ids, counts and timings only; never document content.
        summary = status.get("summary", {})
        log.info("jobId=%s done status=%s chars=%s seconds=%.1f", job_id, status.get("status"),
                 summary.get("totalCharacterCharged"), elapsed)
        (OUT / f"{job_id}_{filename}.status.json").write_text(json.dumps(status, indent=2))

        container = blobs.get_container_client(f"target-{job_id}")
        for blob in container.list_blobs():
            dest = OUT / f"{Path(filename).stem}.{args.target}{Path(blob.name).suffix}"
            dest.write_bytes(container.download_blob(blob.name).readall())
            log.info("jobId=%s downloaded=%s", job_id, dest.name)


if __name__ == "__main__":
    main()
