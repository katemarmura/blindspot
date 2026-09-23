"""Output schema for a single expert's findings.

This is the first domain contract in the project — deliberately small.
No IDs, scores, categories, evidence, or review state yet.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    observation: str
    why_it_matters: str
    assumptions: list[str]
    question_to_check: str

    @field_validator("title", "observation", "why_it_matters", "question_to_check")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("assumptions")
    @classmethod
    def _check_assumptions_nonblank(cls, value: list[str]) -> list[str]:
        return [_require_nonblank(item) for item in value]


class ExpertReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[Finding] = Field(min_length=0, max_length=3)
