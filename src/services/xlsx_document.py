# SPDX-License-Identifier: MIT
"""Translate literal spreadsheet cells while keeping other workbook parts unchanged."""

import io
from copy import deepcopy
from zipfile import ZipFile

from lxml import etree

from services.docx_document import parse

S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def cells(archive):
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        shared = list(parse(archive.read("xl/sharedStrings.xml")).iter(S + "si"))
    for name in archive.namelist():
        if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
            continue
        root = parse(archive.read(name))
        for cell in root.iter(S + "c"):
            if cell.find(S + "f") is not None:
                continue
            kind = cell.get("t")
            value = cell.find(S + "v")
            content = None
            if kind == "s":
                if value is None or value.text is None or not value.text.isdecimal():
                    raise ValueError("Invalid XLSX shared string reference")
                index = int(value.text)
                if index >= len(shared):
                    raise ValueError("Invalid XLSX shared string index")
                content = shared[index]
            elif kind == "inlineStr":
                content = cell.find(S + "is")
            if content is None:
                continue
            nodes = content.xpath("./s:t | ./s:r/s:t", namespaces={"s": S[1:-1]})
            text = "".join(node.text or "" for node in nodes)
            if text.strip():
                address = cell.get("r")
                if not address:
                    raise ValueError("XLSX text cell is missing its address")
                yield name, root, cell, content, text


def extract(data):
    with ZipFile(io.BytesIO(data)) as archive:
        if "xl/workbook.xml" not in archive.namelist():
            raise ValueError("Invalid XLSX workbook")
        if any(name.lower().endswith("vbaproject.bin") for name in archive.namelist()):
            raise ValueError("Macro-enabled workbooks are not supported")
        segments = [
            {"text": text, "role": "paragraph", "location": f"{name}/cell:{cell.get('r')}",
             "part": name, "cell": cell.get("r")}
            for name, _, cell, _, text in cells(archive)
        ]
    return {"segments": segments, "language": "", "pageCount": 0}


def rebuild(data, segments):
    translated = {(s["part"], s["cell"]): s["text"] for s in segments}
    if len(translated) != len(segments):
        raise ValueError("XLSX translation contains duplicate cell identifiers")
    matched, roots = set(), {}
    with ZipFile(io.BytesIO(data)) as original:
        for name, root, cell, content, _ in cells(original):
            key = (name, cell.get("r"))
            if key not in translated:
                raise ValueError("XLSX translation is missing a source cell")
            text = translated[key]
            if len(text.encode("utf-16-le")) // 2 > 32767:
                raise ValueError("Translated XLSX cell exceeds Excel's text limit")
            replacement = deepcopy(content)
            replacement.tag = S + "is"
            nodes = replacement.xpath("./s:t | ./s:r/s:t", namespaces={"s": S[1:-1]})
            nodes[0].text = text
            nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            for node in nodes[1:]:
                node.text = ""
            for phonetic in list(replacement.findall(S + "rPh")):
                replacement.remove(phonetic)
            for child in list(cell):
                if child.tag in (S + "v", S + "is"):
                    cell.remove(child)
            cell.set("t", "inlineStr")
            # Cell extensions must remain after the inline string.
            cell.insert(0, replacement)
            roots[name] = root
            matched.add(key)
        if matched != set(translated):
            raise ValueError("XLSX translation contains unknown cell identifiers")
        output = io.BytesIO()
        with ZipFile(output, "w") as destination:
            for entry in original.infolist():
                content = (etree.tostring(roots[entry.filename], xml_declaration=True, encoding="UTF-8",
                                         standalone=True) if entry.filename in roots
                           else original.read(entry.filename))
                destination.writestr(entry, content)
    return output.getvalue()
