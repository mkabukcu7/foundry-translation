# SPDX-License-Identifier: MIT
import json
import math
import os

from services.config import integer

DEFAULT_WEIGHTS = {"number": 5, "amount": 20, "date": 20, "percentage": 20, "id": 20,
                   "email": 20, "phone": 20, "keep": 20, "map": 10}


def score(findings, reviews):
    weights = {**DEFAULT_WEIGHTS, **json.loads(os.getenv("PASS_A_WEIGHTS_JSON", "{}"))}
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values()):
        raise ValueError("Pass A penalties must be nonnegative numbers")
    penalty = sum(weights[f["type"]] for f in findings if f["match"] == "unmatched")
    llm_score = min((r["score"] for r in reviews), default=0)
    issues = [issue for review in reviews for issue in review["issues"]]
    final = max(0, min(llm_score, 100 - penalty))
    threshold = integer("PASS_THRESHOLD", 85, minimum=0)
    if threshold > 100:
        raise ValueError("PASS_THRESHOLD must not exceed 100")
    decision = "passed" if final >= threshold and not any(i["severity"] == "critical" for i in issues) else "flagged"
    return {"score": final, "decision": decision, "llmScore": llm_score, "passAPenalty": penalty,
            "issues": issues}
