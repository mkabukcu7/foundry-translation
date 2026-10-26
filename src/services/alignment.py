# SPDX-License-Identifier: MIT
"""Align by slide or paragraph/section reading order, never by PDF page number."""

from difflib import SequenceMatcher


def align(source: list[dict], target: list[dict], is_pptx=False) -> list[dict]:
    if is_pptx:
        count = max(len(source), len(target))
        return [{"location": f"slide:{i + 1}", "source": source[i]["text"] if i < len(source) else "",
                 "target": target[i]["text"] if i < len(target) else ""} for i in range(count)]
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
