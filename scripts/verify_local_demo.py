# SPDX-License-Identifier: MIT
"""Verify known synthetic outputs locally, without making Azure calls."""

import argparse
import io
import json
import re
from pathlib import Path

from services import pptx_extract


def verify(folder: Path):
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    presentation = folder / "translated.pptx"
    if presentation.exists():
        text = "\n".join(s["text"] for s in pptx_extract.extract(presentation.read_bytes())["segments"])
        # Also prove that a saved, flagged presentation is still readable.
        from pptx import Presentation
        assert len(Presentation(io.BytesIO(presentation.read_bytes())).slides) == 1
    else:
        text = (folder / "translated.txt").read_text(encoding="utf-8")
    for expected in ("POL-2024-558812", "SIN-2025-004471", "Giulia Rossetti"):
        assert expected in text, "Synthetic identifier or name missing"
    assert "water damage" in text.casefold(), "Expected English translation missing"
    assert re.search(r"EUR\s*500[.,]00", text), "Synthetic amount missing or changed"
    assert report["status"] in ("complete", "needs_review")
    assert report["passB"], "Review results missing"
    assert report["versions"], "Usage metadata missing"
    assert report["costSignals"]["tokens"] > 0
    if any(call.get("agent") == "translation-checker-secondary" for call in report["versions"]):
        assert report["checkerAgreement"], "Independent checker results missing"
    if report["status"] == "needs_review":
        assert "requires review" in text.casefold(), "Flagged output is not marked for review"
    print(json.dumps({"jobId": report["jobId"], "syntheticOutputVerified": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", type=Path, nargs="+")
    args = parser.parse_args()
    for folder in args.folders:
        verify(folder)


if __name__ == "__main__":
    main()
