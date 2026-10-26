# SPDX-License-Identifier: MIT
from functools import lru_cache

from azure.ai.documentintelligence import DocumentIntelligenceClient

from services.config import credential, required


@lru_cache
def client():
    return DocumentIntelligenceClient(required("DOCINTEL_ENDPOINT"), credential(), logging_enable=False)


def extract(data: bytes, model: str = "prebuilt-layout", allow_empty=False) -> dict:
    result = client().begin_analyze_document(
        model, body=data, content_type="application/pdf", features=["languages"], logging_enable=False
    ).result(timeout=600)
    paragraphs = [
        {"text": p.content, "role": str(p.role or "paragraph"), "location": f"paragraph:{i + 1}"}
        for i, p in enumerate(result.paragraphs or [])
    ]
    if not paragraphs:
        paragraphs = [
            {"text": line.content, "role": "paragraph", "location": f"reading-order:{i + 1}"}
            for i, line in enumerate(line for page in result.pages or [] for line in page.lines or [])
        ]
    if not paragraphs and not allow_empty:
        raise ValueError("Document Intelligence extracted no text")
    languages = sorted(result.languages or [], key=lambda item: item.confidence, reverse=True)
    return {"segments": paragraphs, "language": languages[0].locale if languages else ""}
