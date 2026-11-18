# SPDX-License-Identifier: MIT
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from services.config import credential, required


def _local_renderer() -> str | None:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if not executable and sys.platform == "win32":
        for setting in ("ProgramFiles", "ProgramFiles(x86)"):
            directory = os.getenv(setting)
            if directory:
                candidate = Path(directory) / "LibreOffice" / "program" / "soffice.com"
                if candidate.is_file():
                    return str(candidate)
    return executable


def render(data: bytes) -> bytes:
    executable = _local_renderer()
    if executable:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "slides.pptx"
            source.write_bytes(data)
            profile = (source.parent / "profile").as_uri()
            subprocess.run(
                [executable, f"-env:UserInstallation={profile}", "--headless",
                 "--convert-to", "pdf", "--outdir", tmp, str(source)],
                check=True, capture_output=True, timeout=120,
            )
            return (Path(tmp) / "slides.pdf").read_bytes()
    endpoint = required("SLIDE_RENDER_URL")
    if not endpoint.startswith("https://"):
        raise ValueError("SLIDE_RENDER_URL must use HTTPS")
    token = credential().get_token(required("SLIDE_RENDER_SCOPE")).token
    response = requests.post(endpoint, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }, timeout=180, allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        raise ValueError("Slide renderer did not return a PDF")
    if len(response.content) > int(os.getenv("MAX_RENDERED_MB", "80")) * 1024 * 1024:
        raise ValueError("Rendered PDF exceeds the configured limit")
    return response.content
