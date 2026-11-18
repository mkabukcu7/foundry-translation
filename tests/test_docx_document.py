import io
from unittest.mock import MagicMock
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from activities.precheck import validate_document
from scripts import local_demo, showcase
from services import docx_document
from tests.test_showcase import HEADERS, wait_for_job


def docx(extra=""):
    output = io.BytesIO()
    namespace = docx_document.NS["w"]
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml",
                        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                        '<Default Extension="xml" ContentType="application/xml"/>'
                        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                        '</Types>')
        archive.writestr("_rels/.rels",
                        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                        '</Relationships>')
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{namespace}"><w:body>'
                        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Hola</w:t></w:r>'
                        '<w:r><w:t> mundo</w:t></w:r></w:p>'
                        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Importe</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                        f'{extra}<w:sectPr/></w:body></w:document>')
        archive.writestr("word/header1.xml", f'<w:hdr xmlns:w="{namespace}"><w:p><w:r><w:t>Cabecera</w:t>'
                        '</w:r></w:p></w:hdr>')
        archive.writestr("word/media/image1.png", b"unchanged-image")
    return output.getvalue()


def mock_document_translation(monkeypatch):
    def translate_document(data, *_args, **_kwargs):
        segments = docx_document.extract(data)["segments"]
        translated = [{**segment, "text": "English " + segment["text"]} for segment in segments]
        return docx_document.rebuild(data, translated), {}

    monkeypatch.setattr(local_demo.local_translation, "translate_document", translate_document)


def test_docx_preserves_package_and_translates_paragraphs_tables_headers():
    original = docx()
    validate_document(original, "source.docx")
    segments = docx_document.extract(original)["segments"]
    assert [s["text"] for s in segments] == ["Hola mundo", "Importe", "Cabecera"]
    translated = [{**s, "text": t} for s, t in zip(segments, ["Hello world", "Amount", "Header"], strict=True)]
    output = docx_document.rebuild(original, translated, flagged=True)
    with ZipFile(io.BytesIO(output)) as archive, ZipFile(io.BytesIO(original)) as source:
        assert archive.namelist() == source.namelist()
        assert archive.read("word/media/image1.png") == source.read("word/media/image1.png")
        root = docx_document.parse(archive.read("word/document.xml"))
        assert len(root.xpath("//w:tbl", namespaces=docx_document.NS)) == 1
        assert len(root.xpath("//w:b", namespaces=docx_document.NS)) == 1
    texts = [s["text"] for s in docx_document.extract(output)["segments"]]
    assert texts == ["Machine translation - requires review", "Hello world", "Amount", "Header"]


@pytest.mark.parametrize("extra", [
    "<w:p><w:ins><w:r><w:t>Revision</w:t></w:r></w:ins></w:p>",
    '<w:p><w:fldSimple w:instr="PAGE"/></w:p>',
])
def test_docx_rejects_revisions_and_dynamic_fields(extra):
    with pytest.raises(ValueError, match="tracked changes and dynamic fields"):
        validate_document(docx(extra), "source.docx")


@pytest.mark.parametrize("strategy", ["translator", "direct_gpt", "translator_refine"])
@pytest.mark.parametrize("reviewed", [False, True])
def test_docx_workflow_all_strategies_retains_docx_without_review(monkeypatch, tmp_path, strategy, reviewed):
    source = tmp_path / "source.docx"
    source.write_bytes(docx())
    forbidden = MagicMock(side_effect=AssertionError("Review and OCR must not run"))
    monkeypatch.setattr(local_demo, "review_documents", forbidden)
    if reviewed:
        monkeypatch.setattr(local_demo, "review_documents", lambda *_: {
            "decision": "flagged", "score": 80, "issues": [], "costSignals": {},
        })
    monkeypatch.setattr(local_demo.docintel_client, "extract", forbidden)

    def translate(segments, *_args, **_kwargs):
        return {"segments": [{**s, "text": "English " + s["text"]} for s in segments]}, {}

    monkeypatch.setattr(local_demo.local_translation, "translate_segments", translate)
    mock_document_translation(monkeypatch)
    monkeypatch.setattr(local_demo.contextual_translation, "translate", translate)
    report = local_demo.run_file(source, tmp_path / "output", source_language="es",
                                 strategy=strategy, skip_review=not reviewed)
    output = tmp_path / "output" / report["jobId"] / "translated.docx"
    assert report["outputFile"] == "translated.docx"
    assert report["status"] == ("needs_review" if reviewed else "not_reviewed")
    texts = [s["text"] for s in docx_document.extract(output.read_bytes())["segments"]]
    if reviewed:
        assert texts.pop(0) == "Machine translation - requires review"
    assert all(text.startswith("English") for text in texts)
    forbidden.assert_not_called()


def test_ui_docx_upload_and_download(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SHOWCASE_SKIP_REVIEW", "true")
    monkeypatch.setattr(local_demo.local_translation, "translate_segments", lambda segments, *_: (
        {"segments": [{**s, "text": "English"} for s in segments]}, {}))
    monkeypatch.setattr(local_demo.local_translation, "translate_document", lambda data, *_args, **_kwargs: (
        docx_document.rebuild(data, [{**s, "text": "English"} for s in docx_document.extract(data)["segments"]]), {}))
    with TestClient(showcase.create_app(tmp_path), base_url="http://localhost:8765") as client:
        response = client.post("/api/jobs?filename=source.docx&sourceLanguage=es",
                               content=docx(), headers=HEADERS)
        assert response.status_code == 202
        job = wait_for_job(client, response.json()["jobId"])
        assert job["state"] == "finished" and job["output"] == "translated.docx"
        download = client.get(f"/api/jobs/{job['jobId']}/download/translated.docx")
        assert download.status_code == 200
        assert docx_document.extract(download.content)["segments"][0]["text"] == "English"


def test_baseline_docx_has_no_foundry_or_ocr_calls(monkeypatch, tmp_path):
    source = tmp_path / "baseline.docx"
    source.write_bytes(docx())
    forbidden = MagicMock(side_effect=AssertionError("Baseline must not call Foundry or OCR"))
    monkeypatch.setattr(local_demo, "review_documents", forbidden)
    monkeypatch.setattr(local_demo.docintel_client, "extract", forbidden)
    monkeypatch.setattr(local_demo.local_translation, "translate_segments", lambda segments, *_: (
        {"segments": [{**s, "text": "English"} for s in segments]}, {}))
    monkeypatch.setattr(local_demo.local_translation, "translate_document", lambda data, *_args, **_kwargs: (
        docx_document.rebuild(data, [{**s, "text": "English"} for s in docx_document.extract(data)["segments"]]), {}))
    report = local_demo.run_file(source, tmp_path / "output", translator_only=True)
    assert report["outputFile"] == "translated.docx" and report["status"] == "not_reviewed"
    forbidden.assert_not_called()


def test_docx_textbox_text_is_extracted_once_without_duplication():
    extra = ("<w:p><w:r><w:drawing><w:txbxContent><w:p><w:r>"
             "<w:t>Texto interior</w:t></w:r></w:p></w:txbxContent></w:drawing></w:r></w:p>")
    texts = [s["text"] for s in docx_document.extract(docx(extra))["segments"]]
    assert texts.count("Texto interior") == 1
