# SPDX-License-Identifier: MIT
"""Billable agent-only smoke test. Configure the environment and run with PYTHONPATH=src."""

import json
from uuid import uuid4

from services.foundry_agents import invoke, review_pair


def main():
    job_id = uuid4().hex
    source = "La polizza POL-123 prevede una franchigia di EUR 500."
    terms, usage = invoke("term-guard", {
        "text": source, "sourceLanguage": "it", "targetLanguage": "en",
        "standardGlossary": [{"it": "franchigia", "en": "deductible"}],
    }, job_id)
    if not any(t["term"] == "POL-123" and t["action"] == "keep" for t in terms["terms"]):
        raise RuntimeError(f"jobId={job_id}: Term Guard missed the protected identifier")
    print(json.dumps({"jobId": job_id, "step": "term-guard", "termCount": len(terms["terms"]), "usage": usage}))
    for label, amount in (("accurate", "500"), ("altered-amount", "5000")):
        checked = review_pair({
            "location": "paragraph:1", "part": 1, "source": source,
            "target": f"Policy POL-123 has a deductible of EUR {amount}.",
            "glossary": terms["terms"], "passA": [],
        }, job_id)
        for review, cost in checked:
            if label == "accurate" and (review["score"] < 85 or review["issues"]):
                raise RuntimeError(f"jobId={job_id}: Checker rejected the accurate synthetic translation")
            if label == "altered-amount" and not any(
                issue["type"] in ("number", "meaning_shift") for issue in review["issues"]
            ):
                raise RuntimeError(f"jobId={job_id}: Checker missed the altered amount")
            print(json.dumps({
                "jobId": job_id, "step": label, "score": review["score"],
                "issueCount": len(review["issues"]), "usage": cost,
            }))


if __name__ == "__main__":
    main()
