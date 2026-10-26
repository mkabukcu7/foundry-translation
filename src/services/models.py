# SPDX-License-Identifier: MIT
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Term(StrictModel):
    term: str
    type: Literal["policy_number", "claim_number", "name", "address", "amount", "date", "insurance_term", "other"]
    action: Literal["keep", "map"]
    target: str | None


class Terms(StrictModel):
    terms: list[Term]


class Issue(StrictModel):
    severity: Literal["critical", "major", "minor"]
    type: Literal["omission", "addition", "meaning_shift", "terminology", "number", "tone", "formatting"]
    location: str
    source_excerpt: str
    target_excerpt: str
    explanation: str


class Review(StrictModel):
    score: int = Field(ge=0, le=100)
    issues: list[Issue]
