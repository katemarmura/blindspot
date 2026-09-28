"""Domain contracts: a single expert's findings, and an expert-team plan.

Deliberately small. No IDs, scores, categories, evidence, review state,
skills, permissions, or model routing yet.
"""

from typing import Literal

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


class SynthesizedFinding(BaseModel):
    """A consideration synthesized from expert findings, citing its sources by ID.

    `source_finding_ids` must also resolve to supplied findings; that check
    needs the run's source mapping, so it is done by the synthesis stage.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    observation: str
    why_it_matters: str
    assumptions: list[str]
    question_to_check: str
    source_finding_ids: list[str] = Field(min_length=1)

    @field_validator("title", "observation", "why_it_matters", "question_to_check")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("assumptions")
    @classmethod
    def _check_assumptions_nonblank(cls, value: list[str]) -> list[str]:
        return [_require_nonblank(item) for item in value]

    @field_validator("source_finding_ids")
    @classmethod
    def _check_source_ids(cls, value: list[str]) -> list[str]:
        ids = [_require_nonblank(item) for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("source finding IDs must be unique")
        return ids


class SynthesisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[SynthesizedFinding] = Field(min_length=0, max_length=5)


# --- Clarification round ---


def _unique_nonblank_ids(value: list[str], what: str) -> list[str]:
    ids = [_require_nonblank(item) for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{what} must be unique")
    return ids


class ProposedQuestion(BaseModel):
    """A clarification question as proposed by the model, before an ID is assigned."""

    model_config = ConfigDict(extra="forbid")

    question: str
    why_it_matters: str
    source_finding_ids: list[str] = Field(min_length=1)

    @field_validator("question", "why_it_matters")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("source_finding_ids")
    @classmethod
    def _check_sources(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "source finding IDs")


class QuestionBatch(BaseModel):
    """The question-generation call's output: zero to three proposed questions."""

    model_config = ConfigDict(extra="forbid")

    questions: list[ProposedQuestion] = Field(min_length=0, max_length=3)


class ClarificationQuestion(ProposedQuestion):
    """A validated question with its application-assigned, stable ID (e.g. "q1").

    Originating perspectives are derived from `source_finding_ids` in Python,
    not stored here.
    """

    id: str

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _require_nonblank(value)


ResponseStatus = Literal["answered", "dont_know", "not_applicable", "deferred"]


class ResponseEntry(BaseModel):
    """One entry of a user's response file.

    `status` None means "not answered yet" (as in the generated template).
    `answer` is required free text for "answered", an optional explanation for
    "not_applicable", and must be absent for "dont_know", "deferred", and None.
    A blank string counts as absent. `question` is a read-only copy of the
    question text for the person editing the file; it is ignored.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    status: ResponseStatus | None = None
    answer: str | None = None
    question: str | None = None

    @field_validator("question_id")
    @classmethod
    def _check_question_id(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("answer")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        return value if value is not None and value.strip() else None

    @model_validator(mode="after")
    def _check_status_and_answer(self) -> "ResponseEntry":
        if self.status == "answered" and self.answer is None:
            raise ValueError("an 'answered' response requires nonblank answer text")
        if self.status in (None, "dont_know", "deferred") and self.answer is not None:
            shown = self.status or "no status"
            raise ValueError(f"answer text is not allowed with {shown}")
        return self


class ResponseFile(BaseModel):
    """A user-edited response file for one clarification batch.

    Question IDs are checked against the batch (unknown IDs, duplicates) in
    Python, since that needs the clarification artifact.
    """

    model_config = ConfigDict(extra="forbid")

    clarification_id: str
    instructions: str | None = None
    responses: list[ResponseEntry]


class RemainingConsideration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    observation: str
    why_it_matters: str
    source_finding_ids: list[str] = Field(min_length=1)
    question_ids: list[str]

    @field_validator("title", "observation", "why_it_matters")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("source_finding_ids")
    @classmethod
    def _check_sources(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "source finding IDs")

    @field_validator("question_ids")
    @classmethod
    def _check_questions(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "question IDs")


class AddressedConcern(BaseModel):
    """A concern the user's recorded responses bear on. Must cite those questions."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    source_finding_ids: list[str] = Field(min_length=1)
    question_ids: list[str] = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("source_finding_ids")
    @classmethod
    def _check_sources(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "source finding IDs")

    @field_validator("question_ids")
    @classmethod
    def _check_questions(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "question IDs")


class UnresolvedUncertainty(BaseModel):
    """An open uncertainty, traced to findings and/or questions (at least one reference)."""

    model_config = ConfigDict(extra="forbid")

    summary: str
    source_finding_ids: list[str]
    question_ids: list[str]

    @field_validator("summary")
    @classmethod
    def _check_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("source_finding_ids")
    @classmethod
    def _check_sources(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "source finding IDs")

    @field_validator("question_ids")
    @classmethod
    def _check_questions(cls, value: list[str]) -> list[str]:
        return _unique_nonblank_ids(value, "question IDs")

    @model_validator(mode="after")
    def _check_has_reference(self) -> "UnresolvedUncertainty":
        if not self.source_finding_ids and not self.question_ids:
            raise ValueError("an uncertainty must reference a finding or a question")
        return self


class ReassessmentReport(BaseModel):
    """The reassessment call's output. References are validated in Python."""

    model_config = ConfigDict(extra="forbid")

    remaining_considerations: list[RemainingConsideration] = Field(min_length=0, max_length=5)
    addressed_concerns: list[AddressedConcern] = Field(min_length=0, max_length=5)
    unresolved_uncertainties: list[UnresolvedUncertainty] = Field(min_length=0, max_length=5)
