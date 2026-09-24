"""Domain contracts: a single expert's findings, and an expert-team plan.

Deliberately small. No IDs, scores, categories, evidence, review state,
skills, permissions, or model routing yet.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ExpertDefinition(BaseModel):
    """One expert's assignment: a perspective, what to examine, and guiding questions."""

    model_config = ConfigDict(extra="forbid")

    perspective: str
    objective: str
    questions: list[str] = Field(min_length=1, max_length=3)

    @field_validator("perspective", "objective")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("questions")
    @classmethod
    def _check_questions_nonblank(cls, value: list[str]) -> list[str]:
        return [_require_nonblank(item) for item in value]


class TeamPlan(BaseModel):
    """One to three experts with distinct perspectives."""

    model_config = ConfigDict(extra="forbid")

    experts: list[ExpertDefinition] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _check_unique_perspectives(self) -> "TeamPlan":
        seen: set[str] = set()
        for expert in self.experts:
            key = expert.perspective.strip().casefold()
            if key in seen:
                raise ValueError(
                    "expert perspectives must be unique (ignoring case and whitespace)"
                )
            seen.add(key)
        return self
