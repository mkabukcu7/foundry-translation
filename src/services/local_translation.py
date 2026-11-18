# SPDX-License-Identifier: MIT
"""Storage-free Translator adapters. No retries for billable translation requests."""

import io
import os
from urllib.parse import urlparse

import requests
from pptx import Presentation

from services.config import ExternalServiceError, credential, required
from services.pptx_extract import all_shapes, has_images
from services.translation_engines import EngineConfig, config

PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCUMENT_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": PPTX_TYPE,
}


def validate_pptx(data: bytes, *, allow_images=False):
    presentation = Presentation(io.BytesIO(data))
    if not allow_images and has_images(data):
        raise ValueError("Synchronous document translation supports native-text PPTX only")
    for slide in presentation.slides:
        shapes = (slide.shapes, slide.slide_layout.shapes, slide.slide_layout.slide_master.shapes)
        if any(shape.has_chart for group in shapes for shape in all_shapes(group)):
            raise ValueError("Storage-free PPTX mode does not support charts; chart content is not reviewed")


def request(path, job_id, **kwargs):
    endpoint = required("TRANSLATOR_ENDPOINT").rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("TRANSLATOR_ENDPOINT must be an HTTPS resource origin")
    response = requests.post(
        endpoint + path,
        headers={
            "Authorization": f"Bearer {credential().get_token('https://cognitiveservices.azure.com/.default').token}",
            "x-ms-client-request-id": job_id,
        },
        timeout=(10, 180), allow_redirects=False, **kwargs,
    )
    if response.status_code != 200:
        raise ExternalServiceError(f"Translator HTTP {response.status_code}; jobId={job_id}")
    return response


def translate_pptx(data, glossary_data, source_language, target_language, job_id, *, api_version=None):
    return translate_document(
        data, "document.pptx", glossary_data, source_language, target_language, job_id,
        engine=config("translator"),
        api_version=api_version or os.getenv("TRANSLATOR_SYNC_API_VERSION", "2024-05-01"),
    )


def translate_document(
    data: bytes,
    filename: str,
    glossary_data: bytes,
    source_language: str,
    target_language: str,
    job_id: str,
    *,
    engine: EngineConfig,
    api_version: str | None = None,
):
    extension = filename.rsplit(".", 1)[-1].lower()
    extension = f".{extension}" if not extension.startswith(".") else extension
    content_type = DOCUMENT_TYPES.get(extension)
    if content_type is None:
        raise ValueError("Synchronous document translation supports DOCX and PPTX only")
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("Synchronous document translation supports documents up to 10 MB")
    if len(glossary_data) > 1024 * 1024:
        raise ValueError("Synchronous glossary exceeds 1 MB")
    if extension == ".pptx":
        validate_pptx(data)
    files = {"document": (filename, data, content_type)}
    if glossary_data:
        files["glossary"] = ("glossary.tsv", glossary_data, "text/tab-separated-values")
    params = {
        "api-version": api_version or engine.api_version,
        "sourceLanguage": source_language,
        "targetLanguage": target_language,
    }
    if engine.engine == "doc_llm":
        params["deploymentName"] = engine.deployment_name
    response = request("/translator/document:translate", job_id, params=params, files=files)
    if not response.content.startswith(b"PK"):
        raise ExternalServiceError(f"Translator did not return a valid Office document; jobId={job_id}")
    if extension == ".pptx":
        validate_pptx(response.content)
    return response.content, {
        "mode": "synchronous-document",
        "glossaryApplied": bool(glossary_data),
        "charactersCharged": None,
        "imagesTranslated": 0,
        "apiVersion": params["api-version"],
        "maturity": engine.maturity,
    }


def translate_segments(segments, source_language, target_language, job_id):
    translated, submitted, charged = [], 0, []
    for segment in segments:
        text, output = segment["text"], []
        # Bound each request below the 50,000-character limit, without overlap or lost text.
        for start in range(0, len(text), 10000):
            part = text[start:start + 10000]
            response = request("/translator/text/v3.0/translate", job_id, params={
                "api-version": "3.0", "from": source_language, "to": target_language,
            }, json=[{"Text": part}])
            try:
                body = response.json()
            except ValueError:
                raise ExternalServiceError(f"Translator returned invalid JSON; jobId={job_id}") from None
            if (not isinstance(body, list) or len(body) != 1
                    or not isinstance(body[0], dict)
                    or not isinstance(body[0].get("translations"), list)
                    or len(body[0]["translations"]) != 1
                    or not isinstance(body[0]["translations"][0], dict)
                    or body[0]["translations"][0].get("to") != target_language
                    or not isinstance(body[0]["translations"][0].get("text"), str)):
                raise ExternalServiceError(f"Translator returned unexpected text result; jobId={job_id}")
            output.append(body[0]["translations"][0]["text"])
            submitted += len(part)
            metered = response.headers.get("X-Metered-Usage")
            if metered is not None and not metered.isdecimal():
                raise ExternalServiceError(f"Translator returned invalid metered usage; jobId={job_id}")
            charged.append(int(metered) if metered is not None else None)
        translated.append({**segment, "text": "".join(output)})
    return {"segments": translated, "language": target_language}, {
        "mode": "ocr-then-text", "glossaryApplied": False,
        "charactersSubmitted": submitted,
        "charactersCharged": sum(charged) if all(c is not None for c in charged) else None,
        "imagesTranslated": 0,
    }
