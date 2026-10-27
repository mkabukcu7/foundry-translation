# SPDX-License-Identifier: MIT
"""Small live calibration regression, not a document-quality benchmark."""

import json
from pathlib import Path
from uuid import uuid4

from services.foundry_agents import review_pair

SOURCE = "La polizza POL-123 copre i danni da acqua. La franchigia e di EUR 500,00. Contraente: Giulia Rossetti."
TERMS = [
    {"term": "POL-123", "type": "policy_number", "action": "keep", "target": None},
    {"term": "Giulia Rossetti", "type": "name", "action": "keep", "target": None},
    {"term": "EUR 500,00", "type": "amount", "action": "keep", "target": None},
    {"term": "polizza", "type": "insurance_term", "action": "map", "target": "policy"},
    {"term": "franchigia", "type": "insurance_term", "action": "map", "target": "deductible"},
    {"term": "danni da acqua", "type": "insurance_term", "action": "map", "target": "water damage"},
]
GOOD = "Policy POL-123 covers water damage. The deductible is EUR 500.00. Policyholder: Giulia Rossetti."
CASES = [
    ("accurate-localized", GOOD, False),
    ("accurate-source-format", GOOD.replace("500.00", "500,00"), False),
    ("changed-amount", GOOD.replace("500.00", "5000.00"), True),
    ("changed-coverage", GOOD.replace("covers water damage", "does not cover water damage"), True),
    ("changed-policy-number", GOOD.replace("POL-123", "POL-999"), True),
    ("changed-grammatical-number", GOOD.replace("Policy POL-123 covers", "Policies POL-123 cover"), True),
]
EXPECTED = {
    "changed-amount": ({"number", "meaning_shift"}, "5000.00"),
    "changed-coverage": ({"meaning_shift"}, "does not cover"),
    "changed-policy-number": ({"number", "meaning_shift"}, "POL-999"),
    "changed-grammatical-number": ({"meaning_shift"}, "Policies"),
}


def passes(label, review):
    if label not in EXPECTED:
        return not review["issues"] and review["score"] >= 85
    kinds, evidence = EXPECTED[label]
    expected_found = any(issue["type"] in kinds and evidence in issue["target_excerpt"]
                         for issue in review["issues"])
    formatting_false_positive = any(issue["type"] == "formatting" and "500" in issue["target_excerpt"]
                                    for issue in review["issues"])
    return expected_found and not formatting_false_positive


def main():
    job_id = uuid4().hex
    results = []
    for label, target, _defective in CASES:
        for review, usage in review_pair({
            "location": "paragraph:1", "part": 1, "source": SOURCE, "target": target,
            "glossary": TERMS, "passA": [],
        }, job_id):
            passed = passes(label, review)
            results.append({"case": label, "passed": passed, "review": review, "usage": usage})
            print(json.dumps({"jobId": job_id, "case": label, "reviewer": usage["agent"],
                              "passed": passed, "score": review["score"], "issues": len(review["issues"])}))
    destination = Path("samples/results/calibration") / job_id
    destination.mkdir(parents=True)
    (destination / "report.json").write_text(json.dumps({
        "jobId": job_id, "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not all(r["passed"] for r in results):
        raise RuntimeError(f"Calibration regression failed; inspect private report; jobId={job_id}")


if __name__ == "__main__":
    main()
