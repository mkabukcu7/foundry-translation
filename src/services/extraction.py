# SPDX-License-Identifier: MIT
from services import docintel_client, pptx_extract


def extract(data: bytes, filename: str) -> dict:
    result = pptx_extract.extract(data) if filename.lower().endswith(".pptx") else docintel_client.extract(data)
    if not any(s["text"].strip() for s in result["segments"]):
        raise ValueError("Document contains no extractable text")
    return result
