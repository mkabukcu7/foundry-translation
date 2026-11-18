# SPDX-License-Identifier: MIT
"""Rebuild local outputs in the input format, retaining original artwork where possible."""

import io
from collections import Counter
from copy import deepcopy
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt
from pypdf import PdfReader, PdfWriter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from services.config import ReconstructionError
from services.pptx_extract import text_frames


class LayoutError(ReconstructionError):
    """Source geometry cannot accommodate translated text."""


def box(segment, width, height):
    coordinates = segment.get("bounds")
    if (not coordinates or len(coordinates) != 4
            or not 0 <= coordinates[0] < coordinates[2] <= 1
            or not 0 <= coordinates[1] < coordinates[3] <= 1):
        raise LayoutError("Cannot preserve format: text has missing or invalid document coordinates")
    left, top, right, bottom = coordinates
    return left * width, top * height, (right - left) * width, (bottom - top) * height


def paragraph(text, width, height):
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        raise ReconstructionError(
            "Translation completed, but PDF reconstruction cannot render this character set. "
            "The translated text is available as a TXT download."
        ) from None
    for size in range(18, 3, -1):
        content = Paragraph(escape(text).replace("\n", "<br/>"), ParagraphStyle(
            "translation", fontName="Helvetica", fontSize=size, leading=size * 1.1,
        ))
        _, used = content.wrap(width, height)
        if used <= height and all(
            stringWidth(word, "Helvetica", size) <= width
            for word in text.split()
        ):
            return content, used
    raise LayoutError("Translated text does not fit its original region; manual layout editing is required")


def reflow_pdf(segments):
    output = io.BytesIO()
    style = ParagraphStyle("reflow", fontName="Helvetica", fontSize=11, leading=15,
                           splitLongWords=True)
    story = [Paragraph("Reflowed translation - original layout not preserved", style), Spacer(1, 15)]
    for segment in segments:
        # Validate glyph support before ReportLab can silently substitute missing characters.
        paragraph(segment["text"], 1000000, 1000000)
        story.extend([Paragraph(escape(segment["text"]).replace("\n", "<br/>"), style), Spacer(1, 10)])
    SimpleDocTemplate(output).build(story)
    return output.getvalue()


def pdf(data, segments):
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter(clone_from=reader)
    if any(not isinstance(s.get("page"), int) or not 1 <= s["page"] <= len(writer.pages) for s in segments):
        raise LayoutError("Cannot preserve PDF format: text has missing or invalid page coordinates")
    for number, page in enumerate(writer.pages, 1):
        page_segments = [segment for segment in segments if segment["page"] == number]
        if not page_segments:
            continue
        page.transfer_rotation_to_content()
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        output = io.BytesIO()
        drawing = canvas.Canvas(output, pagesize=(width, height))
        for segment in page_segments:
            left, top, w, h = box(segment, width, height)
            content, used = paragraph(segment["text"], w, h)
            drawing.setFillColorRGB(1, 1, 1)
            drawing.rect(left, height - top - h, w, h, fill=1, stroke=0)
            content.drawOn(drawing, left, height - top - used)
        drawing.save()
        page.merge_page(PdfReader(output).pages[0])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def pptx(data, segments, contains_images, *, source_segments=(), image_sidecar=False):
    presentation = Presentation(io.BytesIO(data))
    overlay_regions: Counter[int] = Counter()
    sources = {}
    source_claims = Counter(s.get("location") for s in source_segments)
    if contains_images:
        sources = {s["location"]: s for s in source_segments
                   if s.get("location") and source_claims[s["location"]] == 1}
    for segment_number, segment in enumerate(segments, 1):
        if image_sidecar and "frame" not in segment:
            if segment.get("role") != "image_text":
                raise LayoutError("Cannot preserve PPTX layout: a text segment has no editable shape")
            continue
        page = segment["page"]
        slide = presentation.slides[page - 1]
        if contains_images and "frame" not in segment:
            overlay_regions[page] += 1
            region = f"Slide {page}, OCR region {overlay_regions[page]} (translation segment {segment_number})"
            try:
                left, top, width, height = box(segment, presentation.slide_width, presentation.slide_height)
            except LayoutError as error:
                raise LayoutError(f"{region}: {error}") from None
            text = segment["text"]
            original = sources.get(segment.get("location"))
            if (len(text) == 1 and text.isdecimal() and original is not None
                    and "frame" not in original and original["text"] == text
                    and original["page"] == page and original.get("bounds") == segment["bounds"]):
                # The original glyph is already visible; redrawing it can only lose fidelity.
                continue
            left_pt, top_pt, width_pt, height_pt = (value / 12700 for value in (left, top, width, height))
            try:
                content, _ = paragraph(segment["text"], width_pt, height_pt)
            except LayoutError as error:
                raise LayoutError(
                    f"{region}: {error} (left={left_pt:.1f}, top={top_pt:.1f}, "
                    f"width={width_pt:.1f}, height={height_pt:.1f} pt; "
                    f"{len(segment['text'])} translated characters)"
                ) from None
            shape = slide.shapes.add_textbox(int(left), int(top), int(width), int(height))
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor(255, 255, 255)
            frame = shape.text_frame
            frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
            frame.word_wrap = True
            frame.text = segment["text"]
            for item in frame.paragraphs:
                item.font.size = Pt(content.style.fontSize)
                item.font.color.rgb = RGBColor(0, 0, 0)
        else:
            frame = text_frames(slide)[segment["frame"]]
            target = frame.paragraphs[segment["paragraph"]]
            if target.text == segment["text"]:
                continue
            run_template = deepcopy(target.runs[0]._r) if target.runs else None
            run_properties = run_template.rPr if run_template is not None else None
            end_properties = target._p.find(qn("a:endParaRPr"))
            for child in list(target._p):
                if child.tag not in {qn("a:pPr"), qn("a:endParaRPr")}:
                    target._p.remove(child)

            def append_content(element):
                position = target._p.index(end_properties) if end_properties is not None else len(target._p)
                target._p.insert(position, element)

            lines = segment["text"].replace("\v", "\n").split("\n")
            for index, line in enumerate(lines):
                if index:
                    append_content(OxmlElement("a:br"))
                if line or len(lines) == 1:
                    run = OxmlElement("a:r")
                    if run_properties is not None:
                        run.append(deepcopy(run_properties))
                    text = OxmlElement("a:t")
                    text.text = line
                    if line != line.strip():
                        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    run.append(text)
                    append_content(run)
            if not image_sidecar:
                frame.word_wrap = True
                frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def image(data, segments, extension):
    with Image.open(io.BytesIO(data)) as original:
        frames = []
        for index in range(getattr(original, "n_frames", 1)):
            original.seek(index)
            mode = "RGBA" if original.mode == "RGBA" and extension in (".png", ".tif", ".tiff") else "RGB"
            frame = original.convert(mode)
            drawing = ImageDraw.Draw(frame)
            for segment in segments:
                if segment.get("page") != index + 1:
                    continue
                left, top, width, height = box(segment, *frame.size)
                for size in range(min(80, int(height)), 3, -1):
                    font = ImageFont.load_default(size=size)
                    words, lines, line = segment["text"].split(), [], ""
                    for word in words:
                        candidate = f"{line} {word}".strip()
                        if line and drawing.textlength(candidate, font=font) > width:
                            lines.append(line)
                            line = word
                        else:
                            line = candidate
                    lines.append(line)
                    text = "\n".join(lines)
                    bbox = drawing.multiline_textbbox((0, 0), text, font=font)
                    if bbox[2] <= width and bbox[3] <= height:
                        break
                else:
                    raise ReconstructionError(
                        "Translation completed, but translated image text does not fit. "
                        "Manual layout editing is required; the translated text is available as TXT."
                    )
                drawing.rectangle((left, top, left + width, top + height), fill="white")
                drawing.multiline_text((left, top), text, font=font, fill="black")
            frames.append(frame)
        if any(not isinstance(s.get("page"), int) or not 1 <= s["page"] <= len(frames) for s in segments):
            raise ReconstructionError("Cannot preserve image format: missing or invalid page coordinates")
        output = io.BytesIO()
        format_name = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".bmp": "BMP",
                       ".tif": "TIFF", ".tiff": "TIFF"}[extension]
        options = {"save_all": True, "append_images": frames[1:]} if format_name == "TIFF" else {}
        frames[0].save(output, format=format_name, **options)
        return output.getvalue()
