# SPDX-License-Identifier: MIT
from functools import lru_cache

from azure.ai.documentintelligence import DocumentIntelligenceClient

from services.config import credential, required

IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def bounds(page, polygon):
    xs, ys = polygon[::2], polygon[1::2]
    if not xs or not ys or not page.width or not page.height:
        raise ValueError("OCR text has no usable document coordinates")
    return [min(xs) / page.width, min(ys) / page.height,
            max(xs) / page.width, max(ys) / page.height]


@lru_cache
def client():
    return DocumentIntelligenceClient(required("DOCINTEL_ENDPOINT"), credential(), logging_enable=False)


def extract(data: bytes, model: str = "prebuilt-layout", allow_empty=False, job_id: str = "",
            content_type: str = "application/pdf", positioned=False) -> dict:
    result = client().begin_analyze_document(
        model, body=data, content_type=content_type, features=["languages"], logging_enable=False,
        headers={"x-ms-client-request-id": job_id} if job_id else {},
    ).result(timeout=600)
    paragraphs = [
        {"text": p.content, "role": str(p.role or "paragraph"), "location": f"paragraph:{i + 1}"}
        for i, p in enumerate(result.paragraphs or [])
    ]
    if positioned:
        for segment, paragraph in zip(paragraphs, result.paragraphs or [], strict=True):
            regions = paragraph.bounding_regions or []
            if len(regions) == 1:
                region = regions[0]
                segment["page"] = region.page_number
                segment["bounds"] = bounds(result.pages[region.page_number - 1], region.polygon)
    if not paragraphs or (positioned and any("bounds" not in segment for segment in paragraphs)):
        paragraphs = [
            {"text": line.content, "role": "paragraph", "location": f"reading-order:{i + 1}"}
            for i, line in enumerate(line for page in result.pages or [] for line in page.lines or [])
        ]
        lines = [(page, line) for page in result.pages or [] for line in page.lines or []]
        if positioned:
            for segment, (page, line) in zip(paragraphs, lines, strict=True):
                segment["page"] = page.page_number
                segment["bounds"] = bounds(page, line.polygon)
    if not paragraphs and not allow_empty:
        raise ValueError("Document Intelligence extracted no text")
    languages = sorted(result.languages or [], key=lambda item: item.confidence, reverse=True)
    return {"segments": paragraphs, "language": languages[0].locale if languages else "",
            "pageCount": len(result.pages or [])}
