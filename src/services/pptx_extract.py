# SPDX-License-Identifier: MIT
import io

from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pypdf import PdfReader, PdfWriter

from services import docintel_client, slide_render


def all_shapes(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:
            yield from all_shapes(shape.shapes)


def has_images(data: bytes) -> bool:
    presentation = Presentation(io.BytesIO(data))
    for slide in presentation.slides:
        parts = (slide.part, slide.slide_layout.part, slide.slide_layout.slide_master.part)
        if any(part._element.xpath(".//a:blip") for part in parts):
            return True
        shapes = (slide.shapes, slide.slide_layout.shapes, slide.slide_layout.slide_master.shapes)
        if any(shape.shape_type == 13 or (
            shape.is_placeholder and shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE
        ) for group in shapes for shape in all_shapes(group)):
            return True
    return False


def extract(data: bytes, *, positioned=False) -> dict:
    presentation = Presentation(io.BytesIO(data))
    contains_images = has_images(data)
    if contains_images:
        # Use rendered OCR for the complete slide rather than double-counting native text.
        rendered = PdfReader(io.BytesIO(slide_render.render(data)))
        if len(rendered.pages) != len(presentation.slides):
            raise ValueError("Rendered slide count differs from the presentation")
        segments = []
        for i, page in enumerate(rendered.pages):
            writer, output = PdfWriter(), io.BytesIO()
            writer.add_page(page)
            writer.write(output)
            result = docintel_client.extract(output.getvalue(), "prebuilt-read", allow_empty=True,
                                            **({"positioned": True} if positioned else {}))
            if positioned:
                segments.extend({**segment, "page": i + 1,
                                 "location": f"slide:{i + 1}/{segment['location']}"}
                                for segment in result["segments"])
                continue
            segments.append({
                "text": "\n".join(p["text"] for p in result["segments"]),
                "role": "slide", "location": f"slide:{i + 1}",
            })
        return {"segments": segments, "language": "", "containsImages": True,
                "pageCount": len(segments)}
    segments = []
    for i, slide in enumerate(presentation.slides):
        if positioned:
            frames = [frame for shape in all_shapes(slide.shapes)
                      for frame in ([shape.text_frame] if shape.has_text_frame else [])
                      + ([cell.text_frame for row in shape.table.rows for cell in row.cells]
                         if shape.has_table else [])]
            for frame_index, frame in enumerate(frames):
                for paragraph_index, paragraph in enumerate(frame.paragraphs):
                    if paragraph.text.strip():
                        segments.append({
                            "text": paragraph.text, "role": "paragraph", "page": i + 1,
                            "frame": frame_index, "paragraph": paragraph_index,
                            "location": f"slide:{i + 1}/frame:{frame_index}/paragraph:{paragraph_index}",
                        })
            continue
        text = []
        for shape in all_shapes(slide.shapes):
            if shape.has_text_frame:
                text.append(shape.text_frame.text)
            if shape.has_table:
                text.extend(cell.text for row in shape.table.rows for cell in row.cells)
        segments.append({"text": "\n".join(text), "role": "slide", "location": f"slide:{i + 1}"})
    return {"segments": segments, "language": "", "containsImages": False}
