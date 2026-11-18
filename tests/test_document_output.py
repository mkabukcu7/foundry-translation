import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image
from pptx import Presentation
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches
from pypdf import PdfReader

from services import docintel_client, document_output, pptx_extract
from tests.test_pipeline import pdf


def test_pptx_image_sidecar_ocr_excludes_native_text_and_keeps_original_geometry(monkeypatch):
    presentation = Presentation()
    blank = presentation.slide_layouts[6]
    first_slide = presentation.slides.add_slide(blank)
    first_slide.shapes.add_textbox(0, 0, Inches(2), Inches(1)).text = "First slide"
    inherited = first_slide.shapes.add_textbox(0, 0, Inches(2), Inches(1))
    inherited.text = "Inherited footer"
    first_slide.shapes._spTree.remove(inherited.element)
    blank.shapes._spTree.insert_element_before(inherited.element, "p:extLst")
    slide = presentation.slides.add_slide(blank)
    box = slide.shapes.add_textbox(0, 0, Inches(2), Inches(1))
    box.text = "Original title"
    box.text_frame.word_wrap = False
    table = slide.shapes.add_table(1, 1, 0, Inches(1), Inches(2), Inches(1)).table
    table.cell(0, 0).text = "Original cell"
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "black").save(image, format="PNG")
    image.seek(0)
    picture = slide.shapes.add_picture(image, 0, Inches(2))
    original = io.BytesIO()
    presentation.save(original)

    def render(data):
        rendering = Presentation(io.BytesIO(data))
        assert all(not frame.text for page in rendering.slides for frame in pptx_extract.text_frames(page))
        assert any(shape.has_text_frame and shape.text == "Inherited footer"
                   for shape in rendering.slides[1].slide_layout.shapes)
        assert rendering.slides[1].shapes[2].image.blob == picture.image.blob
        return pdf(2)

    ocr = MagicMock(return_value={
        "segments": [{"text": "Inherited footer Picture label", "location": "reading-order:1"}],
    })
    monkeypatch.setattr(pptx_extract.slide_render, "render", render)
    monkeypatch.setattr(pptx_extract.docintel_client, "extract", ocr)
    extracted = pptx_extract.extract(original.getvalue(), positioned=True, image_sidecar=True, job_id="sample")
    assert extracted["pageCount"] == 1
    assert [(s["page"], s["role"]) for s in extracted["segments"]] == [
        (1, "paragraph"), (2, "paragraph"), (2, "paragraph"), (2, "image_text"),
    ]
    assert extracted["segments"][-1]["text"] == "Inherited footer Picture label"
    ocr.assert_called_once()
    assert ocr.call_args.kwargs["job_id"] == "sample"

    segments = [{**s, "text": f"Translated {s['text']}"} for s in extracted["segments"]]
    rebuilt = Presentation(io.BytesIO(document_output.pptx(
        original.getvalue(), segments, True, image_sidecar=True,
    )))
    title, table_shape, artwork = rebuilt.slides[1].shapes
    assert title.text == "Translated Original title"
    assert title.text_frame.word_wrap is False and title.width == box.width
    assert table_shape.table.cell(0, 0).text == "Translated Original cell"
    assert artwork.image.blob == picture.image.blob


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


def test_pptx_translation_replaces_fields_and_recreates_soft_breaks():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    paragraph = shape.text_frame.paragraphs[0]
    first = paragraph.add_run()
    first.text = "Before"
    first.font.bold = True
    field = OxmlElement("a:fld")
    field.set("id", "{00000000-0000-0000-0000-000000000001}")
    field.set("type", "datetime")
    field_text = OxmlElement("a:t")
    field_text.text = "FIELD"
    field.append(field_text)
    paragraph._p.append(field)
    paragraph.add_line_break()
    paragraph.add_run().text = "After"
    original = io.BytesIO()
    presentation.save(original)

    translated = document_output.pptx(original.getvalue(), [{
        "page": 1, "frame": 0, "paragraph": 0, "text": "Translated\nline",
    }], False)
    rebuilt = Presentation(io.BytesIO(translated))
    output_paragraph = rebuilt.slides[0].shapes[0].text_frame.paragraphs[0]
    assert output_paragraph.text == "Translated\vline"
    assert output_paragraph.runs[0].font.bold
    assert not output_paragraph._p.xpath("./a:fld")
    assert len(output_paragraph._p.xpath(".//a:br")) == 1


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


def test_mixed_slide_reuses_native_text_box_instead_of_tight_ocr_overlay(monkeypatch):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    image = io.BytesIO()
    Image.new("RGB", (200, 200), "blue").save(image, format="PNG")
    picture = slide.shapes.add_picture(io.BytesIO(image.getvalue()), Inches(6), Inches(3))
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    shape.text_frame.text = "Daños"
    original = io.BytesIO()
    presentation.save(original)
    monkeypatch.setattr(pptx_extract.slide_render, "render", lambda _: pdf())
    monkeypatch.setattr(docintel_client, "extract", lambda *_a, **_kw: {
        "segments": [
            {"text": "Daños", "bounds": [.11, .14, .14, .152], "location": "paragraph:1"},
            {"text": "Etiqueta", "bounds": [.61, .41, .81, .46], "location": "paragraph:2"},
        ],
    })
    extracted = pptx_extract.extract(original.getvalue(), positioned=True)
    translations = ["Coverage for damage caused by water", "Label"]
    segments = [{**s, "text": text} for s, text in zip(extracted["segments"], translations, strict=True)]
    with pytest.raises(document_output.LayoutError, match="does not fit"):
        document_output.paragraph(translations[0], 21.6, 6.48)

    rebuilt = Presentation(io.BytesIO(document_output.pptx(original.getvalue(), segments, True)))
    assert len(rebuilt.slides) == 1
    shapes = rebuilt.slides[0].shapes
    assert len(shapes) == 3
    assert shapes[0].image.blob == picture.image.blob
    assert shapes[1].text == translations[0]
    assert shapes[1].text_frame.auto_size == MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    assert (shapes[1].left, shapes[1].top, shapes[1].width, shapes[1].height) == (
        shape.left, shape.top, shape.width, shape.height,
    )
    assert shapes[2].text == "Label"


@pytest.mark.parametrize("bounds,duplicate", [
    ([.61, .41, .81, .46], False),
    ([.11, .14, .14, .152], True),
])
def test_ocr_text_is_not_matched_outside_box_or_with_ambiguous_duplicates(bounds, duplicate):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    shape.text_frame.text = "Policy"
    segments = [{"text": "Policy", "bounds": bounds}]
    if duplicate:
        segments.append({"text": "Policy", "bounds": [.15, .14, .18, .152]})
    pptx_extract._match_editable_text(presentation, slide, segments)
    assert all("frame" not in segment for segment in segments)


def test_repeated_editable_labels_match_by_position():
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    for top in (1, 3):
        slide.shapes.add_textbox(Inches(1), Inches(top), Inches(5), Inches(1)).text_frame.text = "Policy"
    segments = [{"text": "Policy", "bounds": [.11, top, .2, top + .02]} for top in (.41, .15)]
    segments.append({"text": "Policy", "bounds": [.7, .6, .8, .65]})
    pptx_extract._match_editable_text(presentation, slide, segments)
    assert [segment.get("frame") for segment in segments] == [1, 0, None]


@pytest.mark.parametrize("second_top,matched", [(.2, True), (.5, False), (.13, False)])
def test_wrapped_ocr_lines_match_one_editable_paragraph_without_consuming_artwork(second_top, matched):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2))
    shape.text_frame.text = "Water damage coverage"
    segments = [
        {"text": "Water damage", "bounds": [.11, .16, .22, .19], "location": "slide:1/reading-order:1"},
        {"text": "coverage", "bounds": [.11, second_top, .18, second_top + .025]},
        {"text": "Artwork label", "bounds": [.69, .6, .9, .65]},
    ]
    pptx_extract._match_editable_text(presentation, slide, segments)
    if not matched:
        assert len(segments) == 3 and all("frame" not in segment for segment in segments)
        return

    assert segments == [
        {"text": "Water damage coverage", "bounds": [.11, .16, .22, .225],
         "location": "slide:1/reading-order:1", "frame": 0, "paragraph": 0},
        {"text": "Artwork label", "bounds": [.69, .6, .9, .65]},
    ]
    original = io.BytesIO()
    presentation.save(original)
    segments[0].update(page=1, text="Extended water damage coverage for insured buildings")
    segments[1]["page"] = 1
    rebuilt = Presentation(io.BytesIO(document_output.pptx(original.getvalue(), segments, True)))
    assert len(rebuilt.slides) == 1
    assert [s.text for s in rebuilt.slides[0].shapes] == [segment["text"] for segment in segments]
    assert rebuilt.slides[0].shapes[0].width == shape.width


def test_pptx_overlay_overflow_identifies_slide_without_document_text():
    presentation = Presentation()
    for _ in range(2):
        presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = presentation.slides[1].shapes.add_textbox(0, 0, presentation.slide_width, 1000000)
    shape.text = "Source"
    original = io.BytesIO()
    presentation.save(original)
    with pytest.raises(document_output.LayoutError, match=r"Slide 2, OCR region 1") as error:
        document_output.pptx(original.getvalue(), [
            {"page": 2, "frame": 0, "paragraph": 0, "text": "Replacement"},
            {"page": 2, "bounds": [.1, .1, .13, .112], "text": "PRIVATE DOCUMENT CONTENT"},
        ], True)
    message = str(error.value)
    assert "translation segment 2" in message
    assert "left=72.0, top=54.0, width=21.6, height=6.5 pt" in message
    assert "24 translated characters" in message
    assert "PRIVATE" not in message and "Replacement" not in message


def test_pptx_missing_overlay_bounds_reports_region_without_geometry():
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    original = io.BytesIO()
    presentation.save(original)
    with pytest.raises(document_output.LayoutError, match=r"Slide 1, OCR region 1 .*invalid document coordinates"):
        document_output.pptx(original.getvalue(), [{"page": 1, "text": "Artwork"}], True)


@pytest.mark.parametrize("source_change", [None, {"text": "8"}, {"location": "other"},
                                         {"bounds": [.2, .2, .3, .3]}, {"frame": 0}])
def test_pptx_retains_only_an_identical_digit_at_the_same_ocr_region(source_change):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "black").save(image, format="PNG")
    image.seek(0)
    picture = slide.shapes.add_picture(image, 0, 0)
    original = io.BytesIO()
    presentation.save(original)
    digit = {"page": 1, "location": "slide:1/reading-order:1", "text": "0",
             "bounds": [626.7 / 960, 346.7 / 540, 631.7 / 960, 350.5 / 540]}
    label = {"page": 1, "location": "slide:1/reading-order:2", "text": "Translated",
             "bounds": [.1, .1, .3, .2]}
    sources = [{**digit, **(source_change or {})}, {**label, "text": "Original"}]
    if source_change is not None:
        with pytest.raises(document_output.LayoutError, match=r"Slide 1, OCR region 1"):
            document_output.pptx(original.getvalue(), [digit, label], True, source_segments=sources)
        return
    rebuilt = Presentation(io.BytesIO(document_output.pptx(
        original.getvalue(), [digit, label], True, source_segments=sources,
    )))
    assert len(rebuilt.slides) == 1
    artwork, overlay = rebuilt.slides[0].shapes
    assert artwork.image.blob == picture.image.blob
    assert overlay.text == "Translated"


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
