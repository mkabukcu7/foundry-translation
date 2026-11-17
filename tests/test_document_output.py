import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from pypdf import PdfReader

from services import docintel_client, document_output, pptx_extract
from tests.test_pipeline import pdf


def test_pdf_retains_pages_and_outputs_translated_text_in_original_region():
    original = pdf(2)
    segments = [{"page": 1, "bounds": [.1, .1, .7, .3], "text": "The premium increases."},
                {"page": 2, "bounds": [.1, .5, .7, .8], "text": "Water damage is covered."}]
    output = PdfReader(io.BytesIO(document_output.pdf(original, segments)))
    assert len(output.pages) == 2
    assert "The premium increases." in output.pages[0].extract_text()
    assert "Water damage is covered." in output.pages[1].extract_text()
    assert output.pages[0].mediabox == PdfReader(io.BytesIO(original)).pages[0].mediabox


@pytest.mark.parametrize("segment", [
    {"text": "Missing geometry"},
    {"text": "Bad page", "page": 9, "bounds": [.1, .1, .5, .5]},
    {"text": "Bad box", "page": 1, "bounds": [.8, .1, .2, .5]},
    {"text": "x" * 10000, "page": 1, "bounds": [.1, .1, .2, .11]},
])
def test_pdf_reconstruction_fails_explicitly_instead_of_losing_text(segment):
    with pytest.raises(ValueError):
        document_output.pdf(pdf(), [segment])


def test_pdf_reflow_handles_text_that_cannot_fit_and_retains_all_words():
    text = "Expanded translated marketing material. " * 400
    output = PdfReader(io.BytesIO(document_output.reflow_pdf([{"text": text}])))
    extracted = "\n".join(page.extract_text() for page in output.pages)
    assert len(output.pages) > 1
    assert extracted.count("Expanded translated marketing material.") == 400
    assert "original layout not preserved" in extracted


def test_pdf_unicode_failure_is_safe_and_explicit():
    from services.config import ReconstructionError
    with pytest.raises(ReconstructionError, match="character set") as error:
        document_output.reflow_pdf([{"text": "PRIVATE\u2011DOCUMENT"}])
    assert "PRIVATE" not in str(error.value)


def test_native_pptx_replaces_paragraphs_and_tables_without_changing_slides():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    shape.text_frame.paragraphs[0].add_run().text = "Hola"
    shape.text_frame.paragraphs[0].add_run().text = " mundo"
    table = slide.shapes.add_table(1, 1, Inches(1), Inches(3), Inches(5), Inches(1))
    table.table.cell(0, 0).text = "Importe"
    data = io.BytesIO()
    presentation.save(data)
    extracted = pptx_extract.extract(data.getvalue(), positioned=True)
    segments = [{**s, "text": text} for s, text in zip(
        extracted["segments"], ["Hello world", "Amount"], strict=True)]
    output = Presentation(io.BytesIO(document_output.pptx(data.getvalue(), segments, False)))
    assert len(output.slides) == 1
    assert len(output.slides[0].shapes) == 2
    assert output.slides[0].shapes[0].text == "Hello world"
    assert output.slides[0].shapes[1].table.cell(0, 0).text == "Amount"
    assert output.slides[0].shapes[0].left == shape.left


@pytest.mark.parametrize("extension,format_name", [
    (".png", "PNG"), (".jpg", "JPEG"), (".jpeg", "JPEG"), (".bmp", "BMP"),
    (".tif", "TIFF"), (".tiff", "TIFF"),
])
def test_image_reconstruction_preserves_format_dimensions_and_uncovered_artwork(extension, format_name):
    data = io.BytesIO()
    Image.new("RGB", (300, 200), "blue").save(data, format=format_name)
    output = document_output.image(data.getvalue(), [
        {"page": 1, "bounds": [.1, .1, .9, .5], "text": "Special offer"},
    ], extension)
    with Image.open(io.BytesIO(output)) as image:
        assert image.format == format_name
        assert image.size == (300, 200)
        red, green, blue = image.convert("RGB").getpixel((290, 190))
        assert blue > 200 and red < 20 and green < 20
        assert image.crop((30, 20, 270, 100)).getextrema()[0][0] < 100


def test_multipage_tiff_retains_all_frames():
    data = io.BytesIO()
    Image.new("RGB", (200, 200), "blue").save(
        data, format="TIFF", save_all=True, append_images=[Image.new("RGB", (200, 200), "red")])
    output = document_output.image(data.getvalue(), [
        {"page": 2, "bounds": [.1, .1, .9, .7], "text": "Second page"},
    ], ".tiff")
    with Image.open(io.BytesIO(output)) as image:
        assert image.n_frames == 2


def test_pdf_retains_pages_without_ocr_text():
    output = PdfReader(io.BytesIO(document_output.pdf(pdf(2), [
        {"page": 1, "bounds": [.1, .1, .8, .3], "text": "Translation"},
    ])))
    assert len(output.pages) == 2
    assert "Translation" not in output.pages[1].extract_text()


def test_image_slide_keeps_picture_bytes_and_positions_translation(monkeypatch):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    image = io.BytesIO()
    Image.new("RGB", (200, 200), "blue").save(image, format="PNG")
    picture = slide.shapes.add_picture(io.BytesIO(image.getvalue()), 0, 0)
    original = io.BytesIO()
    presentation.save(original)
    from services import slide_render
    monkeypatch.setattr(slide_render, "render", lambda _: pdf())
    monkeypatch.setattr(docintel_client, "extract", lambda *_a, **_kw: {
        "segments": [{"text": "Hola", "page": 1, "bounds": [.1, .1, .8, .3],
                      "location": "paragraph:1"}],
    })
    extracted = pptx_extract.extract(original.getvalue(), positioned=True)
    output = document_output.pptx(
        original.getvalue(), [{**extracted["segments"][0], "text": "Hello"}], True)
    result = Presentation(io.BytesIO(output))
    assert result.slides[0].shapes[0].image.blob == picture.image.blob
    assert result.slides[0].shapes[-1].text == "Hello"
    assert len(result.slides) == 1


def test_ocr_coordinates_are_normalized_and_survive_extraction(monkeypatch):
    page = SimpleNamespace(width=10, height=20, page_number=1)
    paragraph = SimpleNamespace(content="Hola", role=None, bounding_regions=[
        SimpleNamespace(page_number=1, polygon=[1, 2, 8, 2, 8, 4, 1, 4])])
    client = MagicMock()
    client.begin_analyze_document.return_value.result.return_value = SimpleNamespace(
        paragraphs=[paragraph], pages=[page], languages=[])
    monkeypatch.setattr(docintel_client, "client", lambda: client)
    segment = docintel_client.extract(b"synthetic", job_id="geometry-test", positioned=True)["segments"][0]
    assert segment["page"] == 1
    assert segment["bounds"] == [.1, .1, .8, .2]
