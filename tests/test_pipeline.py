import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import jsonschema
import pytest
from pptx import Presentation
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions
from reportlab.pdfgen import canvas

from activities.precheck import validate_document
from activities.route import WATERMARK, watermark
from agents.create_agents import ensure_agent
from services import storage
from services.models import Review, Terms
from services.translator_client import Translator, payload
from tests.helpers import indexed_functions


def pdf(pages=1):
    output = io.BytesIO()
    doc = canvas.Canvas(output)
    for _ in range(pages):
        doc.drawString(50, 50, "Synthetic claim POL-123")
        doc.showPage()
    doc.save()
    return output.getvalue()


def test_pdf_rejects_encrypted_and_copy_restricted():
    validate_document(pdf(), "x.pdf")
    doc, output = PdfWriter(), io.BytesIO()
    doc.append(PdfReader(io.BytesIO(pdf())))
    doc.encrypt("", "owner", permissions_flag=UserAccessPermissions.PRINT, algorithm="AES-256")
    doc.write(output)
    secured = output.getvalue()
    with pytest.raises(ValueError, match="copy-restricted"):
        validate_document(secured, "x.pdf")
    doc, output = PdfWriter(), io.BytesIO()
    doc.append(PdfReader(io.BytesIO(pdf())))
    doc.encrypt("user", "owner", algorithm="AES-256")
    doc.write(output)
    secured = output.getvalue()
    with pytest.raises(ValueError, match="Encrypted"):
        validate_document(secured, "x.pdf")


def test_watermarks_every_page_and_slide():
    result = watermark(pdf(2), "x.pdf")
    pages = PdfReader(io.BytesIO(result)).pages
    assert len(pages) == 2
    assert all(WATERMARK in page.extract_text() and "POL-123" in page.extract_text() for page in pages)
    prs = Presentation()
    for _ in range(2):
        prs.slides.add_slide(prs.slide_layouts[5])
    stream = io.BytesIO()
    prs.save(stream)
    result = Presentation(io.BytesIO(watermark(stream.getvalue(), "x.pptx")))
    assert all(any(WATERMARK in s.text_frame.text for s in slide.shapes if s.has_text_frame)
               for slide in result.slides)


def test_translator_request_exact_options_and_autodetect():
    body = payload("https://a/source/a.pdf", "https://a/target/a.pdf", image_text=True)
    assert body["options"] == {"translateTextWithinImage": True}
    assert "language" not in body["inputs"][0]["source"]
    assert "pdfDocumentIntelligence" not in json.dumps(body)
    assert body["inputs"][0]["storageType"] == "File"
    assert body["inputs"][0]["targets"][0]["targetUrl"].endswith("/a.pdf")


def test_translator_refreshes_tokens_and_rejects_foreign_operations(monkeypatch):
    monkeypatch.setenv("TRANSLATOR_ENDPOINT", "https://unit.cognitiveservices.azure.com")
    adapter = Translator()
    with pytest.raises(ValueError, match="Unexpected"):
        adapter.poll("https://evil.example/translator/document/batches/id", "a" * 32)


@pytest.mark.parametrize("name,model", [("term_guard", Terms), ("checker", Review)])
def test_json_schemas_match_models(name, model):
    schema = json.loads(Path(f"src/schemas/{name}.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    example = {"terms": []} if model is Terms else {"score": 90, "issues": []}
    jsonschema.validate(model.model_validate(example).model_dump(), schema)


def test_agent_creation_is_idempotent(monkeypatch):
    monkeypatch.setenv("FOUNDRY_MODEL_NAME", "model-test")
    from services.foundry_agents import fingerprint
    client = MagicMock()
    client.agents.list_versions.return_value = [
        SimpleNamespace(version="2", metadata={"definitionHash": fingerprint("term-guard")})
    ]
    assert ensure_agent(client, "term-guard") == "2"
    client.agents.create_version.assert_not_called()


def test_app_indexes_expected_routes_and_activities():
    functions = indexed_functions().values()
    names = {f.get_function_name() for f in functions}
    assert {"precheck", "prepare", "translate_start", "translate_poll", "check", "route",
            "notify", "translation_orchestrator", "post_jobs", "get_report", "get_result"} <= names


def test_target_requires_exactly_one_document(monkeypatch):
    client = MagicMock()
    client.get_container_client.return_value.list_blobs.return_value = []
    monkeypatch.setattr(storage, "client", lambda: client)
    with pytest.raises(ValueError, match="one translated"):
        storage.target({"jobId": "a" * 32, "fileName": "test.pdf"})
