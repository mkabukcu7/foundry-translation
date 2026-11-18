# SPDX-License-Identifier: MIT
import io
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from services import docx_document, storage, xlsx_document
from services.config import integer

IMAGE_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".bmp": "BMP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}
MAX_IMAGE_FRAMES = 100
MAX_TOTAL_IMAGE_PIXELS = 40_000_000


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
    elif filename.lower().endswith((".pptx", ".docx", ".xlsx")):
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                is_docx = filename.lower().endswith(".docx")
                is_xlsx = filename.lower().endswith(".xlsx")
                if (is_docx or is_xlsx) and len(data) > 10 * 1024 * 1024:
                    raise ValueError("Office document exceeds the 10 MiB size limit")
                main = "xl/workbook.xml" if is_xlsx else "word/document.xml" if is_docx else "ppt/presentation.xml"
                if main not in archive.namelist():
                    raise ValueError("Invalid Office document")
                if sum(i.file_size for i in archive.infolist()) > integer("MAX_UNCOMPRESSED_MB", 200) * 1024 * 1024:
                    raise ValueError("Office document expanded size exceeds safety limit")
                if is_docx:
                    docx_document.extract(data)
                if is_xlsx:
                    xlsx_document.extract(data)
        except BadZipFile as error:
            raise ValueError("Invalid Office document") from error
    elif filename.lower().endswith(tuple(IMAGE_FORMATS)):
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != IMAGE_FORMATS[next(
                    extension for extension in IMAGE_FORMATS if filename.lower().endswith(extension)
                )]:
                    raise ValueError("Image content does not match its filename extension")
                frame_count = getattr(image, "n_frames", 1)
                if frame_count > MAX_IMAGE_FRAMES:
                    raise ValueError("Image exceeds the maximum frame count")
                total_pixels = 0
                for frame in range(frame_count):
                    image.seek(frame)
                    width, height = image.size
                    if not 50 <= width <= 10000 or not 50 <= height <= 10000:
                        raise ValueError("Image dimensions must be between 50 and 10,000 pixels")
                    total_pixels += width * height
                    if total_pixels > MAX_TOTAL_IMAGE_PIXELS:
                        raise ValueError("Image exceeds the maximum decoded pixel count")
                image.seek(0)
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError("Invalid or unsupported image") from error
    else:
        raise ValueError("Only PDF, PPTX, DOCX, XLSX, JPEG, PNG, BMP and TIFF documents are supported")


def run(job):
    validate_document(storage.source(job), job["fileName"])
    return {"validated": True}
