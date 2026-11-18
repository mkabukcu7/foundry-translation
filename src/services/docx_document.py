# SPDX-License-Identifier: MIT
"""Translate native Word text without rebuilding the Office document package."""

import io
from zipfile import ZipFile

from lxml import etree

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
W = "{" + NS["w"] + "}"
PARTS = ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml")


def parts(archive):
    return [name for name in archive.namelist()
            if name in PARTS or (name.startswith(("word/header", "word/footer")) and name.endswith(".xml"))]


def parse(data):
    return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))


def paragraphs(root):
    for index, paragraph in enumerate(root.iter(W + "p")):
        nodes = [node for node in paragraph.iter(W + "t")
                 if next(node.iterancestors(W + "p")) is paragraph]
        text = "".join(node.text or "" for node in nodes)
        if text.strip():
            yield index, nodes, text


def extract(data):
    segments = []
    with ZipFile(io.BytesIO(data)) as archive:
        if "word/document.xml" not in archive.namelist():
            raise ValueError("Invalid DOCX document")
        for name in parts(archive):
            root = parse(archive.read(name))
            if root.xpath("//w:ins | //w:del | //w:fldChar | //w:fldSimple", namespaces=NS):
                raise ValueError("DOCX tracked changes and dynamic fields must be accepted or converted to text first")
            for index, _, text in paragraphs(root):
                segments.append({"text": text, "role": "paragraph", "location": f"{name}/paragraph:{index}",
                                 "part": name, "paragraph": index})
    return {"segments": segments, "language": "", "pageCount": 0}


def rebuild(data, segments, *, flagged=False):
    translated = {(segment["part"], segment["paragraph"]): segment["text"] for segment in segments}
    if len(translated) != len(segments):
        raise ValueError("DOCX translation contains duplicate paragraph identifiers")
    matched = set()
    output = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as original, ZipFile(output, "w") as destination:
        text_parts = parts(original)
        for entry in original.infolist():
            content = original.read(entry.filename)
            if entry.filename in text_parts:
                root = parse(content)
                for index, nodes, _ in paragraphs(root):
                    key = (entry.filename, index)
                    if key not in translated:
                        raise ValueError("DOCX translation is missing a source paragraph")
                    nodes[0].text = translated[key]
                    nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    for node in nodes[1:]:
                        node.text = ""
                    matched.add(key)
                if flagged and entry.filename == "word/document.xml":
                    body = root.find(W + "body")
                    paragraph = etree.Element(W + "p")
                    run = etree.SubElement(paragraph, W + "r")
                    etree.SubElement(run, W + "t").text = "Machine translation - requires review"
                    body.insert(0, paragraph)
                content = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            destination.writestr(entry, content)
    if matched != set(translated):
        raise ValueError("DOCX translation contains unknown paragraph identifiers")
    return output.getvalue()
