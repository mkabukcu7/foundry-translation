# SPDX-License-Identifier: MIT
import io
from collections import Counter, defaultdict

from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pypdf import PdfReader, PdfWriter

from services import docintel_client, slide_render


def all_shapes(shapes):
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:
            yield from all_shapes(shape.shapes)


def _shape_frames(slide):
    return [
        (shape, frame) for shape in all_shapes(slide.shapes)
        for frame in ([shape.text_frame] if shape.has_text_frame else [])
        + ([cell.text_frame for row in shape.table.rows for cell in row.cells] if shape.has_table else [])
    ]


def text_frames(slide):
    return [frame for _, frame in _shape_frames(slide)]


def _match_editable_text(presentation, slide, segments):
    def key(text):
        return " ".join(text.split())

    candidates = defaultdict(list)
    top_level = {shape.element for shape in slide.shapes}
    # Nested and rotated shapes use different coordinates from rendered OCR.
    for frame_index, (shape, frame) in enumerate(_shape_frames(slide)):
        if not shape.has_text_frame or shape.rotation or shape.element not in top_level:
            continue
        bounds = (
            shape.left / presentation.slide_width,
            shape.top / presentation.slide_height,
            (shape.left + shape.width) / presentation.slide_width,
            (shape.top + shape.height) / presentation.slide_height,
        )
        for paragraph_index, paragraph in enumerate(frame.paragraphs):
            if paragraph.text.strip():
                candidates[key(paragraph.text)].append((frame_index, paragraph_index, bounds))

    prefixes = {text.split(" ", 1)[0] for text in candidates}
    longest = max(map(len, candidates), default=0)
    eligible = []
    for start, segment in enumerate(segments):
        if key(segment["text"]).split(" ", 1)[0] not in prefixes:
            continue
        parts, boxes = [], []
        length = 0
        for end in range(start, len(segments)):
            part = key(segments[end]["text"])
            ocr = segments[end].get("bounds")
            if (not part or not ocr or len(ocr) != 4
                    or (boxes and ocr[1] <= boxes[-1][1])):
                break
            length += len(part) + bool(parts)
            if length > longest:
                break
            parts.append(part)
            boxes.append(ocr)
            for frame_index, paragraph_index, (left, top, right, bottom) in candidates.get(" ".join(parts), ()):
                if all(left <= a < c <= right and top <= b < d <= bottom for a, b, c, d in boxes):
                    eligible.append((range(start, end + 1), (frame_index, paragraph_index)))

    paragraph_claims = Counter(target for _, target in eligible)
    segment_claims = Counter(index for indices, _ in eligible for index in indices)
    consumed = set()
    for indices, target in eligible:
        if paragraph_claims[target] != 1 or any(segment_claims[index] != 1 for index in indices):
            continue
        frame_index, paragraph_index = target
        segment = segments[indices.start]
        segment.update(frame=frame_index, paragraph=paragraph_index)
        if len(indices) > 1:
            segment["text"] = " ".join(key(segments[index]["text"]) for index in indices)
            bounds = [segments[index]["bounds"] for index in indices]
            segment["bounds"] = [min(b[0] for b in bounds), min(b[1] for b in bounds),
                                 max(b[2] for b in bounds), max(b[3] for b in bounds)]
            consumed.update(range(indices.start + 1, indices.stop))
    if consumed:
        segments[:] = [segment for index, segment in enumerate(segments) if index not in consumed]


def _slide_parts(slide):
    return slide.part, slide.slide_layout.part, slide.slide_layout.slide_master.part


def _slide_has_images(slide):
    if any(part._element.xpath(".//a:blip") for part in _slide_parts(slide)):
        return True
    shapes = (slide.shapes, slide.slide_layout.shapes, slide.slide_layout.slide_master.shapes)
    return any(shape.shape_type == 13 or (
        shape.is_placeholder and shape.placeholder_format.type == PP_PLACEHOLDER.PICTURE
    ) for group in shapes for shape in all_shapes(group))


def has_images(data: bytes) -> bool:
    return any(_slide_has_images(slide) for slide in Presentation(io.BytesIO(data)).slides)


def _editable_segments(slide, page):
    return [
        {"text": paragraph.text, "role": "paragraph", "page": page,
         "frame": frame_index, "paragraph": paragraph_index,
         "location": f"slide:{page}/frame:{frame_index}/paragraph:{paragraph_index}"}
        for frame_index, frame in enumerate(text_frames(slide))
        for paragraph_index, paragraph in enumerate(frame.paragraphs)
        if paragraph.text.strip()
    ]


def _extract_image_sidecar(presentation, *, job_id):
    by_page = [_editable_segments(slide, page) for page, slide in enumerate(presentation.slides, 1)]
    image_pages = [index for index, slide in enumerate(presentation.slides) if _slide_has_images(slide)]
    if image_pages:
        # Clear editable text only in this temporary rendering copy, never in the output.
        for slide in presentation.slides:
            for node in slide.part._element.xpath(".//a:p/a:r/a:t | .//a:p/a:fld/a:t"):
                node.text = ""
        temporary = io.BytesIO()
        presentation.save(temporary)
        rendered = PdfReader(io.BytesIO(slide_render.render(temporary.getvalue())))
        if len(rendered.pages) != len(by_page):
            raise ValueError("Rendered slide count differs from the presentation")
        for index in image_pages:
            writer, output = PdfWriter(), io.BytesIO()
            writer.add_page(rendered.pages[index])
            writer.write(output)
            result = docintel_client.extract(output.getvalue(), "prebuilt-read", allow_empty=True, job_id=job_id)
            by_page[index].extend(
                {**segment, "page": index + 1, "role": "image_text",
                 "location": f"slide:{index + 1}/image/{segment['location']}"}
                for segment in result["segments"]
            )
    return {"segments": [segment for page in by_page for segment in page], "language": "",
            "containsImages": bool(image_pages), "pageCount": len(image_pages)}


def extract(data: bytes, *, positioned=False, image_sidecar=False, job_id="") -> dict:
    presentation = Presentation(io.BytesIO(data))
    if image_sidecar:
        return _extract_image_sidecar(presentation, job_id=job_id)
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
                slide_segments = [
                    {**segment, "page": i + 1, "location": f"slide:{i + 1}/{segment['location']}"}
                    for segment in result["segments"]
                ]
                _match_editable_text(presentation, presentation.slides[i], slide_segments)
                segments.extend(slide_segments)
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
            segments.extend(_editable_segments(slide, i + 1))
            continue
        text = []
        for shape in all_shapes(slide.shapes):
            if shape.has_text_frame:
                text.append(shape.text_frame.text)
            if shape.has_table:
                text.extend(cell.text for row in shape.table.rows for cell in row.cells)
        segments.append({"text": "\n".join(text), "role": "slide", "location": f"slide:{i + 1}"})
    return {"segments": segments, "language": "", "containsImages": False}
