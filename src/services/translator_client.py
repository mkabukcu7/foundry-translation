# SPDX-License-Identifier: MIT
"""2026-03-01 contract: azure-rest-api-specs DocumentTranslation stable OpenAPI."""

from urllib.parse import urlparse

import requests

from services.config import ExternalServiceError, credential, required

API_VERSION = "2026-03-01"
TERMINAL = {"Succeeded", "Failed", "Cancelled", "ValidationFailed"}


def payload(source_url, target_url, target_language="en", source_language="", glossary_url=None,
            image_text=False):
    source = {"sourceUrl": source_url}
    if source_language:
        source["language"] = source_language
    target = {"targetUrl": target_url, "language": target_language}
    if glossary_url:
        target["glossaries"] = [{"glossaryUrl": glossary_url, "format": "TSV"}]
    return {
        "inputs": [{"storageType": "File", "source": source, "targets": [target]}],
        "options": {"translateTextWithinImage": image_text},
    }


class Translator:
    def __init__(self):
        self.base = required("TRANSLATOR_ENDPOINT").rstrip("/") + "/translator/document/batches"

    def request(self, method, url, job_id, **kwargs):
        expected, actual = urlparse(self.base), urlparse(url)
        if (actual.scheme != "https" or actual.netloc != expected.netloc
                or not (actual.path == expected.path or actual.path.startswith(expected.path + "/"))):
            raise ValueError("Unexpected Translator operation URL")
        response = requests.request(
            method, url, headers={
                "Authorization": f"Bearer {credential().get_token('https://cognitiveservices.azure.com/.default').token}",
                "x-ms-client-request-id": job_id,
            }, timeout=60, allow_redirects=False, **kwargs,
        )
        if not 200 <= response.status_code < 300:
            # Error bodies may contain customer document names/content; don't log or propagate them.
            raise ExternalServiceError(f"Translator HTTP {response.status_code}; jobId={job_id}")
        return response

    def submit(self, job, source_url, target_url, glossary_url=None):
        response = self.request("POST", f"{self.base}?api-version={API_VERSION}", job["jobId"], json=payload(
            source_url, target_url, job["targetLanguage"], job.get("sourceLanguage", ""), glossary_url,
            job["fileName"].lower().endswith(".pptx"),
        ))
        if response.status_code != 202 or "Operation-Location" not in response.headers:
            raise ExternalServiceError("Translator did not return an accepted operation")
        return response.headers["Operation-Location"]

    def poll(self, operation_url, job_id):
        return self.request("GET", operation_url, job_id).json()

    def documents(self, operation_url, job_id):
        url = operation_url.split("?")[0] + f"/documents?api-version={API_VERSION}"
        documents = []
        while url:
            response = self.request("GET", url, job_id).json()
            documents.extend(response["value"])
            url = response.get("@nextLink") or response.get("nextLink")
        return documents

    def detect(self, text, job_id):
        url = required("TRANSLATOR_ENDPOINT").rstrip("/") + "/translator/text/v3.0/detect?api-version=3.0"
        response = requests.post(url, json=[{"Text": text[:10000]}], headers={
            "Authorization": f"Bearer {credential().get_token('https://cognitiveservices.azure.com/.default').token}",
            "x-ms-client-request-id": job_id,
        }, timeout=60, allow_redirects=False)
        if response.status_code != 200:
            raise ExternalServiceError(f"Translator language detection HTTP {response.status_code}")
        return response.json()[0]["language"]
