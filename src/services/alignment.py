# SPDX-License-Identifier: MIT
"""Align by slide or paragraph/section reading order, never by PDF page number."""

from difflib import SequenceMatcher


def align(source: list[dict], target: list[dict], is_pptx=False) -> list[dict]:
    if is_pptx:
        source_text, target_text = {}, {}
        for segments, index in ((source, source_text), (target, target_text)):
            for segment in segments:
                index.setdefault(segment["location"], []).append(segment["text"])
        return [
            {"location": location, "source": "\n".join(source_text.get(location, [])),
             "target": "\n".join(target_text.get(location, []))}
            for location in dict.fromkeys([*source_text, *target_text])
        ]
    # Anchor role sequences; aggregate insertions/deletions so reflow is not mistaken for page loss.
    matcher = SequenceMatcher(a=[s["role"] for s in source], b=[s["role"] for s in target], autojunk=False)
    pairs = []
    for _, a, b, c, d in matcher.get_opcodes():
        if b - a == d - c:
            pairs.extend({"location": source[i]["location"], "source": source[i]["text"],
                          "target": target[c + i - a]["text"]} for i in range(a, b))
        else:
            pairs.append({"location": f"section:{a + 1}-{b}",
                          "source": "\n".join(s["text"] for s in source[a:b]),
                          "target": "\n".join(s["text"] for s in target[c:d])})
    return pairs
