# SPDX-License-Identifier: MIT
import io
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from services import storage
from services.config import integer


def validate_document(data: bytes, filename: str):
    if len(data) > min(integer("MAX_DOCUMENT_MB", 40), 40) * 1024 * 1024:
        raise ValueError("Document exceeds maximum document size")
    if filename.lower().endswith(".pdf"):
        try:
            document = PdfReader(io.BytesIO(data), strict=True)
            # PDF permission restrictions are encoded in an encryption dictionary, even with a blank user password.
            if document.is_encrypted:
                raise ValueError("Encrypted or copy-restricted PDFs cannot be translated; upload an unsecured PDF")
            if len(document.pages) == 0:
                raise ValueError("PDF has no pages")
        except PdfReadError as error:
            raise ValueError("Invalid PDF document") from error
    elif filename.lower().endswith(".pptx"):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                if "ppt/presentation.xml" not in archive.namelist():
                    raise ValueError("Invalid PPTX presentation")
                if sum(i.file_size for i in archive.infolist()) > integer("MAX_UNCOMPRESSED_MB", 200) * 1024 * 1024:
                    raise ValueError("PPTX expanded size exceeds safety limit")
        except BadZipFile as error:
            raise ValueError("Invalid PPTX presentation") from error
    else:
        raise ValueError("Only PDF and PPTX documents are supported; convert .ppt to .pptx")


def run(job):
    validate_document(storage.source(job), job["fileName"])
    return {"validated": True}
