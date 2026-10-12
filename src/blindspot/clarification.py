"""Clarification over a saved `--auto-team` run: questions, reassessment, one follow-up.

Run with:
    uv run python -m blindspot.clarification prepare --run runs/team-<id>
    # edit runs/team-<id>/clarify-<id>/responses.json
    uv run python -m blindspot.clarification reassess \\
        --clarification runs/team-<id>/clarify-<id>/clarification.json \\
        --responses runs/team-<id>/clarify-<id>/responses.json
    # optionally, one follow-up round (round 2):
    uv run python -m blindspot.clarification followup \\
        --reassessment runs/team-<id>/clarify-<id>/reassessment-<id>.json
    # edit the new clarify-<id2>/responses.json, then reassess it the same way

`prepare` loads the run's `result.json`, makes one structured-output call for
up to three questions (citing original finding IDs), and writes a new
`clarify-*` directory with `clarification.json` and an editable
`responses.json` template. `reassess` validates both files and makes one call
that reassesses the original findings in light of the recorded responses,
saved as a new `reassessment-*.json`. `followup` takes a successful round-1
reassessment and makes one call for up to three follow-up questions, saved as
a new round-2 `clarify-*` directory that records its lineage and the round-1
questions and answers; reassessing it uses the answers from both rounds.

Each command makes at most one model call: no recovery, retries, or repair.
Original run files and earlier clarification files are never modified, and
experts are never rerun. Every transition is manual; there is no automatic
loop, and a third round is rejected.
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anthropic
import pydantic
from anthropic.types import Message, TextBlock
from pydantic import BaseModel, ConfigDict, Field, model_validator

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.expert import (
    REQUEST_TIMEOUT_SECONDS,
    ExpertError,
    FailureDiagnostics,
    FailureStage,
    UsageSummary,
    ValidationIssue,
    ValidationSchema,
    _call,
    _check_stop_reason,
    _describe_validation_error,
    _response_text,
    _validation_issues,
)
from blindspot.prompts import (
    CLARIFICATION_QUESTIONS_SYSTEM_PROMPT,
    FOLLOWUP_QUESTIONS_SYSTEM_PROMPT,
    REASSESSMENT_SYSTEM_PROMPT,
)
from blindspot.runs import create_run_dir, git_state, utc_now, write_json
from blindspot.schemas import (
    ClarificationQuestion,
    ConclusionInsight,
    ExpertDefinition,
    ExpertReport,
    QuestionBatch,
    ReassessmentReport,
    ReassessmentSections,
    ResponseEntry,
    ResponseFile,
    ResponseStatus,
    SavedReassessmentReport,
    SynthesisReport,
)
from blindspot.synthesis import ExpertInput, SourceRef, build_input, source_map

QUESTIONS_MAX_OUTPUT_TOKENS = 15000
REASSESSMENT_MAX_OUTPUT_TOKENS = 15000

ARTIFACT_FILENAME = "clarification.json"
TEMPLATE_FILENAME = "responses.json"


class ClarificationInputError(ValueError):
    """A saved run, clarification artifact, or response file is missing or invalid."""


class ClarificationError(ExpertError):
    """A clarification-round model call failed; carries usage, model, and diagnostics.

    `call` holds the call-level observations (`CallDiagnostics`), including
    elapsed time, for any failure after the call was attempted.
    """

    def __init__(
        self,
        message: str,
        usage: UsageSummary | None = None,
        returned_model: str | None = None,
        diagnostics: FailureDiagnostics | None = None,
        call: "CallDiagnostics | None" = None,
    ) -> None:
        super().__init__(message, usage, returned_model, diagnostics)
        self.call = call


# --- Loading the saved auto-team run ---


class _SavedExpert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["success", "failed"]
    report: ExpertReport | None
    assignment: ExpertDefinition


class _SavedSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expert_position: int
    finding_position: int


class _SavedSynthesis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    report: SynthesisReport | None = None


class _SavedRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    plan: str
    experts: list[_SavedExpert]
    sources: dict[str, _SavedSource] | None = None
    synthesis: _SavedSynthesis | None = None


@dataclass(frozen=True)
class InvestigationContext:
    """The original investigation, as needed to ask questions and reassess."""

    plan: str
    experts: list[ExpertInput]
    sources: dict[str, SourceRef]
    synthesis: SynthesisReport | None


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ClarificationInputError(f"{what} not found: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClarificationInputError(f"{what} could not be read as JSON: {path}") from exc


def _validate(model: type[BaseModel], data: Any, what: str) -> Any:
    try:
        return model.model_validate(data)
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        raise ClarificationInputError(f"{what} is invalid ({details})") from exc


def load_run(run_dir: Path) -> tuple[str, InvestigationContext]:
    """Load and check a saved `--auto-team` result; return (run_id, context).

    Source IDs are recomputed from the saved expert reports (so older runs
    without a saved `sources` mapping still work) and, when a mapping was
    saved, checked against it. A completed synthesis is included only if all
    its citations resolve.
    """
    saved: _SavedRun = _validate(
        _SavedRun, _read_json(run_dir / "result.json", "Run result"), "Run result"
    )
    experts: list[ExpertInput] = []
    for position, expert in enumerate(saved.experts, start=1):
        if (expert.status == "success") != (expert.report is not None):
            raise ClarificationInputError(
                f"Run result is inconsistent: expert {position} status does not match its report"
            )
        experts.append(ExpertInput(position, expert.assignment, expert.report))

    sources = source_map(experts)
    if saved.sources is not None:
        saved_positions = {
            sid: (ref.expert_position, ref.finding_position) for sid, ref in saved.sources.items()
        }
        derived = {sid: (r.expert_position, r.finding_position) for sid, r in sources.items()}
        if saved_positions != derived:
            raise ClarificationInputError(
                "Run result is inconsistent: saved source IDs do not match the expert reports"
            )

    synthesis = None
    if saved.synthesis is not None and saved.synthesis.status == "completed":
        synthesis = saved.synthesis.report
        if synthesis is not None and any(
            sid not in sources for f in synthesis.findings for sid in f.source_finding_ids
        ):
            raise ClarificationInputError(
                "Run result is inconsistent: synthesis cites unknown source IDs"
            )
    return saved.run_id, InvestigationContext(saved.plan, experts, sources, synthesis)


def question_perspectives(
    question: ClarificationQuestion, sources: dict[str, SourceRef]
) -> list[str]:
    """Originating perspectives of a question, derived from its cited findings (team order)."""
    refs = sorted(
        (sources[sid] for sid in question.source_finding_ids if sid in sources),
        key=lambda r: r.expert_position,
    )
    return list(dict.fromkeys(r.perspective for r in refs))


# --- One structured call (shared by both stages) ---


@dataclass
class CallDiagnostics:
    """Content-free observations about one clarification-round call.

    Response-dependent fields stay None when no response arrived (e.g. an API
    error), rather than implying an empty response. Only block *types* and
    text *lengths* are kept — never text, thinking content, or signatures.
    `parse_text_is_valid_json` is JSON syntax only (schema validation is
    separate) and None when there was no text to parse. Token counts are the
    API's reported usage; nothing is inferred from text length.
    """

    requested_model: str
    max_output_tokens: int
    usage: UsageSummary
    elapsed_seconds: float | None = None
    response_received: bool = False
    returned_model: str | None = None
    stop_reason: str | None = None
    content_blocks: dict[str, int] | None = None
    text_characters: int | None = None
    parse_text_is_valid_json: bool | None = None

    @property
    def model(self) -> str | None:
        return self.returned_model

    def record_response(self, response: Message) -> None:
        self.response_received = True
        self.returned_model = response.model
        self.stop_reason = response.stop_reason
        self.content_blocks = dict(Counter(block.type for block in response.content))
        self.text_characters = sum(
            len(block.text) for block in response.content if isinstance(block, TextBlock)
        )
        text = _response_text(response)
        self.parse_text_is_valid_json = None if text is None else _is_json(text)

    def to_json(self) -> dict[str, Any]:
        return {
            "requested_model": self.requested_model,
            "max_output_tokens": self.max_output_tokens,
            "elapsed_seconds": (
                round(self.elapsed_seconds, 3) if self.elapsed_seconds is not None else None
            ),
            "response_received": self.response_received,
            "returned_model": self.returned_model,
            "stop_reason": self.stop_reason,
            "content_blocks": self.content_blocks,
            "text_characters": self.text_characters,
            "parse_text_is_valid_json": self.parse_text_is_valid_json,
            "usage": _usage_json(self.usage),
        }


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


async def _structured_call[T: BaseModel](
    *,
    stage: FailureStage,
    schema_name: ValidationSchema,
    system: str,
    user_content: str,
    output_schema: type[T],
    max_tokens: int,
    check: Callable[[T], tuple[ValidationIssue, ...]],
) -> tuple[T, CallDiagnostics]:
    """One call, validated against `output_schema` and then by `check` (reference checks).

    `check(parsed)` returns a tuple of `ValidationIssue`s; any issue is a
    failure. Raises `ClarificationError` with usage, model, diagnostics, and
    `CallDiagnostics`; returns the parsed output and `CallDiagnostics` on success.
    """
    api_key = get_anthropic_api_key()
    model = get_anthropic_model()
    usage = UsageSummary()
    call = CallDiagnostics(requested_model=model, max_output_tokens=max_tokens, usage=usage)
    label = {
        "clarification_questions": "Question generation",
        "followup_questions": "Follow-up question generation",
    }.get(stage, "Reassessment")
    start = time.monotonic()

    async with anthropic.AsyncAnthropic(
        api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
    ) as client:
        try:
            response = await _call(
                client,
                usage,
                model=model,
                max_tokens=max_tokens,
                system=system,
                user_content=user_content,
                output_schema=output_schema,
                stage=stage,
            )
        except ExpertError as exc:
            # No response: response-dependent fields stay unavailable.
            call.elapsed_seconds = time.monotonic() - start
            raise ClarificationError(
                f"{label} failed: {exc}", exc.usage, diagnostics=exc.diagnostics, call=call
            ) from exc
    call.elapsed_seconds = time.monotonic() - start
    call.record_response(response)

    def failure(
        message: str, issues: tuple[ValidationIssue, ...] | None = None
    ) -> ClarificationError:
        return ClarificationError(
            message,
            usage,
            returned_model=response.model,
            diagnostics=FailureDiagnostics(
                failure_stage=stage, stop_reason=response.stop_reason, validation_errors=issues
            ),
            call=call,
        )

    try:
        _check_stop_reason(response, usage, stage, prefix=f"{label} failed: ")
    except ExpertError as exc:
        raise ClarificationError(
            str(exc),
            exc.usage,
            returned_model=response.model,
            diagnostics=exc.diagnostics,
            call=call,
        ) from exc
    text = _response_text(response)
    if text is None:
        raise failure(f"{label} returned no output.")
    try:
        parsed = output_schema.model_validate_json(text)
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        raise failure(
            f"{label} output was invalid ({details})", _validation_issues(schema_name, exc)
        ) from exc
    issues = check(parsed)
    if issues:
        # Unknown references are model output, so only their positions are reported.
        raise failure(f"{label} output cited {len(issues)} invalid reference(s)", issues)
    return parsed, call


def _usage_json(usage: UsageSummary | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "model_calls": usage.model_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "usage_incomplete": usage.calls_missing_usage > 0,
    }


def _call_fields(call: CallDiagnostics | None, error: ClarificationError | None) -> dict[str, Any]:
    """Model, elapsed time, and usage for this stage's call only, plus full call diagnostics."""
    usage = call.usage if call is not None else (error.usage if error is not None else None)
    return {
        "model": call.returned_model if call is not None else None,
        "elapsed_seconds": (
            round(call.elapsed_seconds, 3)
            if call is not None and call.elapsed_seconds is not None
            else None
        ),
        "usage": _usage_json(usage),
        "call_diagnostics": call.to_json() if call is not None else None,
    }


def _error_fields(error: ClarificationError | None) -> dict[str, Any]:
    return {
        "error": str(error) if error is not None else None,
        "diagnostics": (
            error.diagnostics.to_json()
            if error is not None and error.diagnostics is not None
            else None
        ),
    }


# --- Stage 1: prepare questions ---


class _SavedResearchSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    url: str
    title: str | None = None
    retrieved_at: str | None = None
    page_age: str | None = None


class _SavedEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_id: str
    quote: str


class _SavedResearchFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_statement: str
    interpretation: str
    applicability_limits: str
    evidence: list[_SavedEvidence] = Field(min_length=1)


class _SavedResearchQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    question: str
    source_finding_ids: list[str]
    status: str
    supported_findings: list[_SavedResearchFinding]
    missing_information: list[dict[str, Any]]
    conflict_notes: list[str]
    limitations: list[str]


class ResearchContent(BaseModel):
    """The validated evidence of one research artifact, as supplied to later stages.

    A follow-up (round 2) artifact keeps a copy as `prior_research`, so the
    evidence behind round-1 research citations travels with the batch.
    """

    model_config = ConfigDict(extra="forbid")

    research_id: str
    research_file: str
    sources: list[_SavedResearchSource]
    questions: list[_SavedResearchQuestion]

    @property
    def source_ids(self) -> set[str]:
        return {source.id for source in self.sources}

    @property
    def citation_source_ids(self) -> set[str]:
        """Fetched sources backed by at least one accepted evidence excerpt."""
        return self.source_ids & {
            e.source_id for q in self.questions for f in q.supported_findings for e in f.evidence
        }


def _research_citations(report: ReassessmentSections) -> set[str]:
    """Every research source ID a report cites (items and structured takeaway)."""
    sections: list[list[Any]] = [
        report.remaining_considerations,
        report.addressed_concerns,
        report.unresolved_uncertainties,
    ]
    cited = {rid for items in sections for item in items for rid in item.research_source_ids}
    takeaway = getattr(report, "takeaway", None)
    if isinstance(takeaway, ConclusionInsight):
        cited |= set(takeaway.research_source_ids)
    return cited


class _ArtifactExpert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    assignment: ExpertDefinition
    report: ExpertReport | None


class ClarificationLineage(BaseModel):
    """Where a follow-up (round 2) batch came from: the round-1 batch and reassessment."""

    model_config = ConfigDict(extra="forbid")

    clarification_id: str
    clarification_file: str
    reassessment_id: str
    reassessment_file: str


# Fields only a follow-up (round 2) artifact uses; omitted from round-1 files so
# their saved format is unchanged.
_ROUND_2_FIELDS = {
    "round",
    "previous",
    "prior_questions",
    "prior_responses",
    "prior_unanswered_question_ids",
    "prior_reassessment",
    "prior_research",
}


class ClarificationArtifact(BaseModel):
    """`clarification.json`: the original context, questions, and call metadata.

    Holds everything reassessment needs, so it doesn't depend on the run's
    files. Source IDs are re-derived from `experts` when loaded; the saved
    `sources` are for inspection.

    A follow-up batch (`round` 2) also records its lineage (`previous`) and
    copies of the round-1 questions, the recorded responses its reassessment
    used, the questions left unanswered, and that reassessment's report. Its
    own `questions` are only the new ones, with IDs continuing after round 1.
    Only one follow-up round is supported.
    """

    model_config = ConfigDict(extra="forbid")

    clarification_id: str
    created_at: str
    status: Literal["success", "failed"]
    source_run: dict[str, str]
    plan: str
    experts: list[_ArtifactExpert]
    synthesis: SynthesisReport | None
    sources: dict[str, dict[str, Any]]
    questions: list[ClarificationQuestion]
    question_generation: dict[str, Any]
    round: Literal[1, 2] = 1
    previous: ClarificationLineage | None = None
    prior_questions: list[ClarificationQuestion] = Field(default_factory=list)
    prior_responses: list[ResponseEntry] = Field(default_factory=list)
    prior_unanswered_question_ids: list[str] = Field(default_factory=list)
    prior_reassessment: SavedReassessmentReport | None = None
    # The research the round-1 reassessment used, if any; omitted when there is none.
    prior_research: ResearchContent | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _check_unique_question_ids(self) -> "ClarificationArtifact":
        # Responses are matched to questions by ID, so a duplicate (within a
        # round or across rounds) would attach one answer to more than one question.
        ids = [q.id for q in self.all_questions]
        duplicates = sorted({qid for qid in ids if ids.count(qid) > 1})
        if duplicates:
            raise ValueError(f"duplicate question IDs: {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _check_round(self) -> "ClarificationArtifact":
        prior = (
            self.previous,
            self.prior_questions,
            self.prior_responses,
            self.prior_unanswered_question_ids,
            self.prior_reassessment,
            self.prior_research,
        )
        if self.round == 1:
            if any(prior):
                raise ValueError("a round-1 artifact cannot carry earlier-round data")
            return self
        if self.previous is None or self.prior_reassessment is None or not self.prior_questions:
            raise ValueError(
                "a follow-up artifact needs its lineage, prior questions, and prior reassessment"
            )
        if any(r.status is None for r in self.prior_responses):
            raise ValueError("prior responses must all have a status")
        # Every prior question must appear exactly once: either with its recorded
        # response or as unanswered. Otherwise earlier answers could silently vanish.
        accounted = sorted(
            [r.question_id for r in self.prior_responses] + self.prior_unanswered_question_ids
        )
        if accounted != sorted(q.id for q in self.prior_questions):
            raise ValueError(
                "each prior question must appear exactly once, in prior_responses or "
                "prior_unanswered_question_ids"
            )
        # Research citations in the copied report must resolve against the copied evidence.
        cited = _research_citations(self.prior_reassessment)
        available = (
            self.prior_research.citation_source_ids if self.prior_research is not None else set()
        )
        if not cited <= available:
            raise ValueError(
                "the prior reassessment cites research sources that prior_research does not hold"
            )
        return self

    @property
    def all_questions(self) -> list[ClarificationQuestion]:
        return [*self.prior_questions, *self.questions]

    def to_json(self) -> dict[str, Any]:
        exclude = _ROUND_2_FIELDS if self.round == 1 else set()
        data: dict[str, Any] = self.model_dump(mode="json", exclude=exclude)
        return data

    def context(self) -> InvestigationContext:
        experts = [ExpertInput(e.position, e.assignment, e.report) for e in self.experts]
        return InvestigationContext(self.plan, experts, source_map(experts), self.synthesis)


def _questions_input(context: InvestigationContext) -> str:
    content = build_input(context.plan, context.experts)
    if context.synthesis is not None:
        synthesis = json.dumps(context.synthesis.model_dump(), indent=2, ensure_ascii=False)
        content += (
            "\n\nExisting synthesis of these findings (derived from them; not independent "
            f"evidence):\n{synthesis}"
        )
    return content


def _question_reference_issues(
    batch: QuestionBatch, sources: dict[str, SourceRef]
) -> tuple[ValidationIssue, ...]:
    return tuple(
        ValidationIssue(
            schema="question_batch",
            loc=f"questions.{i}.source_finding_ids.{j}",
            type="unknown_source_id",
        )
        for i, question in enumerate(batch.questions)
        for j, sid in enumerate(question.source_finding_ids)
        if sid not in sources
    )


@dataclass(frozen=True)
class PreparedClarification:
    directory: Path
    artifact: ClarificationArtifact
    call: CallDiagnostics | None
    error: ClarificationError | None


def _template(artifact: ClarificationArtifact, sources: dict[str, SourceRef]) -> dict[str, Any]:
    return {
        "clarification_id": artifact.clarification_id,
        "instructions": (
            "For each question set status to one of: answered (answer required), dont_know, "
            "not_applicable (answer optional, as an explanation), deferred. Leave status null "
            "to leave a question unanswered. 'question' (with its perspectives in brackets) "
            "is for reference and is ignored."
        ),
        "responses": [
            {
                "question_id": q.id,
                "question": f"{q.question} [{', '.join(question_perspectives(q, sources))}]",
                "status": None,
                "answer": None,
            }
            for q in artifact.questions
        ],
    }


async def prepare(run_dir: Path) -> PreparedClarification:
    """Generate one batch of up to three questions for a saved auto-team run.

    Raises `ClarificationInputError` (before any call and before creating
    anything) for a missing/invalid run or one without successful findings.
    A failed call is saved as a `status: "failed"` artifact and returned.
    """
    run_id, context = load_run(run_dir)
    if not context.sources:
        raise ClarificationInputError(
            "The run has no successful expert findings to ask about; no call was made."
        )
    get_anthropic_api_key()
    requested_model = get_anthropic_model()

    clarification_id, directory = create_run_dir("clarify-", runs_dir=run_dir)
    generation: dict[str, Any] = {
        "requested_model": requested_model,
        "system_prompt": CLARIFICATION_QUESTIONS_SYSTEM_PROMPT,
        "max_output_tokens": QUESTIONS_MAX_OUTPUT_TOKENS,
        "git": git_state(),
    }
    questions: list[ClarificationQuestion] = []
    call: CallDiagnostics | None = None
    error: ClarificationError | None = None
    try:
        batch, call = await _structured_call(
            stage="clarification_questions",
            schema_name="question_batch",
            system=CLARIFICATION_QUESTIONS_SYSTEM_PROMPT,
            user_content=_questions_input(context),
            output_schema=QuestionBatch,
            max_tokens=QUESTIONS_MAX_OUTPUT_TOKENS,
            check=lambda parsed: _question_reference_issues(parsed, context.sources),
        )
        # IDs are assigned only after the batch and its references are valid.
        questions = [
            ClarificationQuestion(id=f"q{i}", **q.model_dump())
            for i, q in enumerate(batch.questions, start=1)
        ]
    except ClarificationError as exc:
        error = exc
        call = exc.call

    generation.update(
        status="failed" if error is not None else "success",
        **_call_fields(call, error),
        **_error_fields(error),
    )
    artifact = ClarificationArtifact(
        clarification_id=clarification_id,
        created_at=utc_now(),
        status="failed" if error is not None else "success",
        source_run={"directory": str(run_dir), "run_id": run_id},
        plan=context.plan,
        experts=[
            _ArtifactExpert(position=e.position, assignment=e.assignment, report=e.report)
            for e in context.experts
        ],
        synthesis=context.synthesis,
        sources={sid: ref.to_json() for sid, ref in context.sources.items()},
        questions=questions,
        question_generation=generation,
    )
    write_json(directory / ARTIFACT_FILENAME, artifact.to_json())
    if error is None:
        write_json(directory / TEMPLATE_FILENAME, _template(artifact, context.sources))
    return PreparedClarification(directory, artifact, call, error)


# --- Stage 2: reassess ---


@dataclass(frozen=True)
class RecordedResponses:
    """Validated responses: those with a status, and the questions left unanswered."""

    recorded: list[ResponseEntry]
    unanswered: list[str]

    @property
    def has_new_context(self) -> bool:
        return any(r.status != "deferred" for r in self.recorded)

    def status_of(self, question_id: str) -> ResponseStatus | None:
        return next((r.status for r in self.recorded if r.question_id == question_id), None)


def load_artifact(path: Path) -> ClarificationArtifact:
    artifact: ClarificationArtifact = _validate(
        ClarificationArtifact, _read_json(path, "Clarification artifact"), "Clarification artifact"
    )
    if artifact.status != "success":
        raise ClarificationInputError(
            "Clarification artifact records a failed question-generation call; run prepare again"
        )
    context = artifact.context()
    for q in artifact.all_questions:
        if any(sid not in context.sources for sid in q.source_finding_ids):
            raise ClarificationInputError(
                f"Clarification artifact is inconsistent: {q.id} cites unknown source IDs"
            )
    return artifact


def load_responses(path: Path, artifact: ClarificationArtifact) -> RecordedResponses:
    """Validate a response file against the artifact's questions.

    Rejects a different clarification ID, unknown question IDs, duplicate
    responses, and invalid status/answer combinations. Questions without a
    response (omitted, or status null) are unanswered.
    """
    responses: ResponseFile = _validate(
        ResponseFile, _read_json(path, "Response file"), "Response file"
    )
    if responses.clarification_id != artifact.clarification_id:
        raise ClarificationInputError(
            "Response file belongs to a different clarification batch "
            f"(expected clarification_id {artifact.clarification_id!r})"
        )
    question_ids = [q.id for q in artifact.questions]
    seen: set[str] = set()
    for position, entry in enumerate(responses.responses):
        if entry.question_id not in question_ids:
            raise ClarificationInputError(
                f"Response file is invalid (responses.{position}: unknown question ID)"
            )
        if entry.question_id in seen:
            raise ClarificationInputError(
                f"Response file is invalid (responses.{position}: duplicate response for "
                f"{entry.question_id})"
            )
        seen.add(entry.question_id)
    by_id = {e.question_id: e for e in responses.responses if e.status is not None}
    recorded = [by_id[qid] for qid in question_ids if qid in by_id]
    unanswered = [qid for qid in question_ids if qid not in by_id]
    return RecordedResponses(recorded, unanswered)


def _response_json(entry: ResponseEntry) -> dict[str, Any]:
    return {"question_id": entry.question_id, "status": entry.status, "answer": entry.answer}


def _accumulated(artifact: ClarificationArtifact, current: RecordedResponses) -> RecordedResponses:
    """Round 1: the current responses. Round 2: saved round-1 responses plus current ones."""
    if artifact.round == 1:
        return current
    return RecordedResponses(
        [*artifact.prior_responses, *current.recorded],
        [*artifact.prior_unanswered_question_ids, *current.unanswered],
    )


def _accumulated_json(
    artifact: ClarificationArtifact, current: RecordedResponses
) -> list[dict[str, Any]]:
    """Responses from every round, each tagged with its round (round-2 artifacts only)."""
    return [
        *({**_response_json(r), "round": 1} for r in artifact.prior_responses),
        *({**_response_json(r), "round": artifact.round} for r in current.recorded),
    ]


def _reassessment_input(artifact: ClarificationArtifact, responses: RecordedResponses) -> str:
    context = artifact.context()

    def dump(value: object) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False)

    if artifact.round == 2:
        prior_ids = {q.id for q in artifact.prior_questions}
        questions = [
            {
                **q.model_dump(),
                "round": 1 if q.id in prior_ids else 2,
                "perspectives": question_perspectives(q, context.sources),
            }
            for q in artifact.all_questions
        ]
        combined = _accumulated(artifact, responses)
        return (
            f"{build_input(context.plan, context.experts)}\n\n"
            f"Clarification questions from both rounds:\n{dump(questions)}\n\n"
            "Recorded responses from both rounds (reported by the user; not independently "
            f"verified):\n{dump(_accumulated_json(artifact, responses))}\n\n"
            f"Unanswered questions (no response recorded):\n{dump(combined.unanswered)}"
        )

    questions = [
        {**q.model_dump(), "perspectives": question_perspectives(q, context.sources)}
        for q in artifact.questions
    ]
    return (
        f"{build_input(context.plan, context.experts)}\n\n"
        f"Clarification questions:\n{dump(questions)}\n\n"
        "Recorded responses (reported by the user; not independently verified):\n"
        f"{dump([_response_json(r) for r in responses.recorded])}\n\n"
        f"Unanswered questions (no response recorded):\n{dump(responses.unanswered)}"
    )


def _reassessment_reference_issues(
    report: ReassessmentSections,
    artifact: ClarificationArtifact,
    responses: RecordedResponses,
    research_ids: set[str] | None = None,
) -> tuple[ValidationIssue, ...]:
    """Reference checks. `research_ids` are the supplied research artifact's source IDs
    (None when no research was supplied, so any research citation is invalid)."""
    sources = artifact.context().sources
    question_ids = {q.id for q in artifact.all_questions}
    issues: list[ValidationIssue] = []

    def issue(loc: str, kind: str) -> None:
        issues.append(ValidationIssue(schema="reassessment_report", loc=loc, type=kind))

    def check_research_ids(loc: str, ids: list[str]) -> None:
        for j, rid in enumerate(ids):
            if research_ids is None:
                issue(f"{loc}.research_source_ids.{j}", "research_source_without_research")
            elif rid not in research_ids:
                issue(f"{loc}.research_source_ids.{j}", "unknown_research_source_id")

    takeaway = getattr(report, "takeaway", None)
    if isinstance(takeaway, ConclusionInsight):
        check_research_ids("takeaway", takeaway.research_source_ids)

    sections: list[tuple[str, list[Any]]] = [
        ("remaining_considerations", report.remaining_considerations),
        ("addressed_concerns", report.addressed_concerns),
        ("unresolved_uncertainties", report.unresolved_uncertainties),
    ]
    for section, items in sections:
        for i, item in enumerate(items):
            check_research_ids(f"{section}.{i}", item.research_source_ids)
            for j, sid in enumerate(item.source_finding_ids):
                if sid not in sources:
                    issue(f"{section}.{i}.source_finding_ids.{j}", "unknown_source_id")
            for j, qid in enumerate(item.question_ids):
                if qid not in question_ids:
                    issue(f"{section}.{i}.question_ids.{j}", "unknown_question_id")
                elif section == "addressed_concerns" and responses.status_of(qid) not in (
                    "answered",
                    "not_applicable",
                ):
                    # Only an actual answer (or a not-applicable assessment) can address a concern.
                    issue(f"{section}.{i}.question_ids.{j}", "question_not_answered")
    return tuple(issues)


def _sha256(value: object) -> str:
    """A deterministic hash of JSON-compatible data (sorted keys, no whitespace)."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# What each snapshot key covers, for a precise mismatch message.
_SNAPSHOT_LABELS = {
    "clarification_id": "clarification batch",
    "clarification_round": "clarification round",
    "plan_sha256": "plan",
    "findings_sha256": "expert findings",
    "question_ids": "question IDs",
    "questions_sha256": "question wording",
    "recorded_responses": "recorded answers",
    "unanswered_question_ids": "unanswered questions",
}


def answers_snapshot(
    artifact: ClarificationArtifact, responses: RecordedResponses
) -> dict[str, Any]:
    """The exact research-relevant inputs: batch, plan, findings, questions, and answers.

    The plan, the expert findings (each expert's assignment and report), and the full
    questions of every round are hashed; recorded answers are kept verbatim. Research
    saves this; reassessment recomputes it and requires an exact match, so evidence is
    never applied to another plan, finding set, question wording, or answer set.
    """
    recorded = (
        _accumulated_json(artifact, responses)
        if artifact.round == 2
        else [_response_json(r) for r in responses.recorded]
    )
    return {
        "clarification_id": artifact.clarification_id,
        "clarification_round": artifact.round,
        "plan_sha256": hashlib.sha256(artifact.plan.encode("utf-8")).hexdigest(),
        "findings_sha256": _sha256([e.model_dump(mode="json") for e in artifact.experts]),
        "question_ids": [q.id for q in artifact.all_questions],
        "questions_sha256": _sha256([q.model_dump(mode="json") for q in artifact.all_questions]),
        "recorded_responses": recorded,
        "unanswered_question_ids": _accumulated(artifact, responses).unanswered,
    }


def _snapshot_differences(saved: dict[str, Any], current: dict[str, Any]) -> list[str]:
    keys = [*_SNAPSHOT_LABELS, *sorted((set(saved) | set(current)) - set(_SNAPSHOT_LABELS))]
    return [_SNAPSHOT_LABELS.get(k, k) for k in keys if saved.get(k) != current.get(k)]


class _SavedResearch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    research_id: str
    status: Literal["completed", "failed"]
    input_snapshot: dict[str, Any]
    sources: list[_SavedResearchSource]
    questions: list[_SavedResearchQuestion]


@dataclass(frozen=True)
class ResearchEvidence:
    """Validated research evidence supplied to a later stage.

    `carried_forward` marks round-1 research travelling with a follow-up batch:
    it was made for the round-1 answers, not for answers recorded since.
    """

    content: ResearchContent
    carried_forward: bool = False

    @property
    def research_id(self) -> str:
        return self.content.research_id

    @property
    def source_ids(self) -> set[str]:
        return self.content.source_ids

    @property
    def citation_source_ids(self) -> set[str]:
        return self.content.citation_source_ids

    def link(self) -> dict[str, Any]:
        """The lineage block saved in a reassessment that used this evidence."""
        return {
            "research_id": self.research_id,
            "research_file": self.content.research_file,
            "source_ids": sorted(self.source_ids),
            **({"carried_forward_from_round": 1} if self.carried_forward else {}),
        }

    def section(self) -> str:
        data = {
            "sources": [
                source.model_dump()
                for source in self.content.sources
                if source.id in self.citation_source_ids
            ],
            "citation_source_ids": sorted(self.citation_source_ids),
            "questions": [
                {
                    "question": q.question,
                    "source_finding_ids": q.source_finding_ids,
                    "status": q.status,
                    "supported_findings": [f.model_dump() for f in q.supported_findings],
                    "missing_information": q.missing_information,
                    "conflict_notes": q.conflict_notes,
                    "limitations": q.limitations,
                }
                for q in self.content.questions
            ],
        }
        origin = (
            " It was made for the first-round recorded answers; answers recorded since may "
            "change how it applies, and where they differ the user's answers take precedence."
            if self.carried_forward
            else ""
        )
        return (
            f"\n\nExternal research evidence (research {self.research_id}).{origin} Quotes were "
            "checked against text retrieved from the listed public pages; source_statement, "
            "interpretation, and applicability_limits are the research model's reading, not "
            "verified facts about this user. Retrieval time is not proof that a rule is current. "
            "Only citation_source_ids may be cited; other fetched pages supplied no verified "
            "evidence. Conflict notes are context, not independently verified evidence. "
            "Questions with insufficient evidence, conflicting sources, failed retrieval, or "
            "missing user information remain open uncertainty, not evidence of no concern:\n"
            f"{json.dumps(data, indent=2, ensure_ascii=False)}"
        )


def load_research_evidence(
    path: Path, artifact: ClarificationArtifact, responses: RecordedResponses
) -> ResearchEvidence:
    """Validate a research artifact for this batch and answer set, before any call."""
    saved: _SavedResearch = _validate(
        _SavedResearch, _read_json(path, "Research artifact"), "Research artifact"
    )
    if saved.status != "completed":
        raise ClarificationInputError(
            "Research artifact records a failed research stage and cannot be used as evidence; "
            "reassess without --research or run research again. No call was made."
        )
    differences = _snapshot_differences(saved.input_snapshot, answers_snapshot(artifact, responses))
    if differences:
        raise ClarificationInputError(
            "Research artifact was made from different inputs (differs in: "
            f"{', '.join(differences)}); it cannot be applied here. No call was made."
        )
    source_ids = {source.id for source in saved.sources}
    cited = {e.source_id for q in saved.questions for f in q.supported_findings for e in f.evidence}
    if not cited <= source_ids:
        raise ClarificationInputError(
            "Research artifact is inconsistent: evidence cites unknown source IDs. "
            "No call was made."
        )
    content = ResearchContent(
        research_id=saved.research_id,
        research_file=str(path),
        sources=saved.sources,
        questions=saved.questions,
    )
    return ResearchEvidence(content)


@dataclass(frozen=True)
class Reassessment:
    path: Path | None
    report: ReassessmentReport | None
    call: CallDiagnostics | None
    error: ClarificationError | None
    responses: RecordedResponses
    # The batch's round, and (round 2) the round-1 reassessment it follows.
    round: int = 1
    previous_reassessment_file: str | None = None


def _round_info(artifact: ClarificationArtifact) -> tuple[int, str | None]:
    previous = artifact.previous.reassessment_file if artifact.previous is not None else None
    return artifact.round, previous


def _conclusion_status(artifact: ClarificationArtifact, error: ClarificationError | None) -> Any:
    """What a successful reassessment represents: the current assessment after round 1, or
    the conclusion of this bounded review after round 2. None when the call failed."""
    if error is not None:
        return None
    return "current_assessment" if artifact.round == 1 else "review_conclusion"


async def reassess(
    artifact_path: Path, responses_path: Path, research_path: Path | None = None
) -> Reassessment:
    """Reassess once with the recorded responses; save to a new `reassessment-*.json`.

    `research_path` optionally supplies a research artifact as external evidence; it
    must come from this batch, its findings and questions, and exactly these recorded
    answers, or it is rejected before any call. A follow-up batch that carries the
    round-1 research uses it automatically and refuses another research artifact.
    Raises `ClarificationInputError` for invalid files. Makes no call (and saves
    nothing) when every response is missing or deferred.
    """
    artifact = load_artifact(artifact_path)
    responses = load_responses(responses_path, artifact)
    research: ResearchEvidence | None = None
    if artifact.prior_research is not None:
        if research_path is not None:
            raise ClarificationInputError(
                "This follow-up batch already carries the round-1 research "
                f"({artifact.prior_research.research_id}), which is included automatically. "
                "Combining it with another research artifact is not supported; reassess "
                "without --research. No call was made."
            )
        research = ResearchEvidence(artifact.prior_research, carried_forward=True)
    elif research_path is not None:
        research = load_research_evidence(research_path, artifact, responses)
    if not responses.has_new_context:
        return Reassessment(None, None, None, None, responses, *_round_info(artifact))
    get_anthropic_api_key()
    requested_model = get_anthropic_model()

    report: ReassessmentReport | None = None
    call: CallDiagnostics | None = None
    error: ClarificationError | None = None
    try:
        report, call = await _structured_call(
            stage="reassessment",
            schema_name="reassessment_report",
            system=REASSESSMENT_SYSTEM_PROMPT,
            user_content=_reassessment_input(artifact, responses)
            + (research.section() if research is not None else ""),
            output_schema=ReassessmentReport,
            max_tokens=REASSESSMENT_MAX_OUTPUT_TOKENS,
            check=lambda parsed: _reassessment_reference_issues(
                parsed,
                artifact,
                _accumulated(artifact, responses),
                research.citation_source_ids if research is not None else None,
            ),
        )
    except ClarificationError as exc:
        error = exc
        call = exc.call

    if artifact.round == 1:
        round_fields: dict[str, Any] = {
            "recorded_responses": [_response_json(r) for r in responses.recorded],
            "unanswered_question_ids": responses.unanswered,
        }
    else:
        # Every round's responses, each tagged with its round, so the file shows
        # exactly which answers this reassessment used.
        round_fields = {
            "clarification_round": artifact.round,
            "recorded_responses": _accumulated_json(artifact, responses),
            "unanswered_question_ids": _accumulated(artifact, responses).unanswered,
        }

    reassessment_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    path = artifact_path.parent / f"reassessment-{reassessment_id}.json"
    if path.exists():  # never overwrite an earlier reassessment
        raise ClarificationInputError(f"Refusing to overwrite existing file: {path}")
    write_json(
        path,
        {
            "reassessment_id": reassessment_id,
            "created_at": utc_now(),
            "clarification_id": artifact.clarification_id,
            "clarification_file": str(artifact_path),
            "responses_file": str(responses_path),
            "status": "failed" if error is not None else "success",
            "conclusion_status": _conclusion_status(artifact, error),
            **round_fields,
            **({"research": research.link()} if research is not None else {}),
            "report": report.model_dump() if report is not None else None,
            "requested_model": requested_model,
            **_call_fields(call, error),
            **_error_fields(error),
            "settings": {
                "system_prompt": REASSESSMENT_SYSTEM_PROMPT,
                "max_output_tokens": REASSESSMENT_MAX_OUTPUT_TOKENS,
                "git": git_state(),
            },
        },
    )
    return Reassessment(path, report, call, error, responses, *_round_info(artifact))


# --- Follow-up round (round 2) ---


class _SavedResponse(BaseModel):
    """A response as saved in a reassessment file (older and newer formats)."""

    model_config = ConfigDict(extra="ignore")

    question_id: str
    status: ResponseStatus
    answer: str | None = None


class _ResearchLink(BaseModel):
    """The `research` block a reassessment saves when it used research."""

    model_config = ConfigDict(extra="ignore")

    research_id: str
    research_file: str
    source_ids: list[str]


class _SavedReassessment(BaseModel):
    """The parts of a saved `reassessment-*.json` a follow-up round depends on."""

    model_config = ConfigDict(extra="ignore")

    reassessment_id: str
    clarification_id: str
    clarification_file: str | None = None
    clarification_round: int = 1
    status: Literal["success", "failed"]
    recorded_responses: list[_SavedResponse]
    unanswered_question_ids: list[str]
    # Older reports have no takeaway; they still load and can start a follow-up.
    report: SavedReassessmentReport | None = None
    # The research this reassessment used, if any (absent in older files).
    research: _ResearchLink | None = None


@dataclass(frozen=True)
class FollowupSource:
    """A validated round-1 batch and the successful reassessment a follow-up builds on."""

    reassessment_path: Path
    reassessment: _SavedReassessment
    artifact_path: Path
    artifact: ClarificationArtifact
    responses: RecordedResponses
    # The research the reassessment used, re-validated against its batch and answers.
    research: ResearchEvidence | None = None


def _artifact_path_for(reassessment_path: Path, saved: _SavedReassessment) -> Path:
    candidates = [reassessment_path.parent / ARTIFACT_FILENAME]
    if saved.clarification_file:
        candidates.insert(0, Path(saved.clarification_file))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ClarificationInputError(
        f"Clarification artifact for this reassessment not found (tried {candidates[0]})"
    )


def load_followup_source(reassessment_path: Path) -> FollowupSource:
    """Validate everything a follow-up round needs, before any call or file is created.

    Requires a successful round-1 reassessment whose clarification ID matches
    its artifact, whose saved responses cover exactly that batch's questions,
    and whose report references resolve. A round-2 reassessment or artifact is
    rejected: only one follow-up round is supported.
    """
    saved: _SavedReassessment = _validate(
        _SavedReassessment, _read_json(reassessment_path, "Reassessment"), "Reassessment"
    )
    if saved.clarification_round != 1:
        raise ClarificationInputError(
            "This reassessment is already from a follow-up round; only one follow-up round "
            "is supported, so no further questions can be generated. No call was made."
        )
    if saved.status != "success" or saved.report is None:
        raise ClarificationInputError(
            "A follow-up needs a successful reassessment with a report; this one failed. "
            "No call was made."
        )
    artifact_path = _artifact_path_for(reassessment_path, saved)
    artifact = load_artifact(artifact_path)
    if artifact.round != 1:
        raise ClarificationInputError(
            "This reassessment belongs to a follow-up batch; only one follow-up round is "
            "supported. No call was made."
        )
    if saved.clarification_id != artifact.clarification_id:
        raise ClarificationInputError(
            "Reassessment and clarification artifact do not match (different "
            "clarification_id). No call was made."
        )
    question_ids = [q.id for q in artifact.questions]
    answered = [r.question_id for r in saved.recorded_responses]
    if len(set(answered)) != len(answered) or not set(answered) <= set(question_ids):
        raise ClarificationInputError(
            "Reassessment's recorded responses do not match the batch's questions. "
            "No call was made."
        )
    if set(answered) | set(saved.unanswered_question_ids) != set(question_ids) or (
        set(answered) & set(saved.unanswered_question_ids)
    ):
        raise ClarificationInputError(
            "Reassessment's answered and unanswered questions do not cover the batch exactly. "
            "No call was made."
        )
    try:
        recorded = [
            ResponseEntry(question_id=r.question_id, status=r.status, answer=r.answer)
            for r in saved.recorded_responses
        ]
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        raise ClarificationInputError(
            f"Reassessment's recorded responses are invalid ({details}). No call was made."
        ) from exc
    responses = RecordedResponses(recorded, list(saved.unanswered_question_ids))
    research = (
        _followup_research(reassessment_path, saved.research, artifact, responses)
        if saved.research is not None
        else None
    )
    if _reassessment_reference_issues(
        saved.report,
        artifact,
        responses,
        research.citation_source_ids if research is not None else None,
    ):
        raise ClarificationInputError(
            "Reassessment report cites findings, questions, or research sources that do not "
            "resolve against its batch and research. No call was made."
        )
    return FollowupSource(reassessment_path, saved, artifact_path, artifact, responses, research)


def _followup_research(
    reassessment_path: Path,
    link: _ResearchLink,
    artifact: ClarificationArtifact,
    responses: RecordedResponses,
) -> ResearchEvidence:
    """Reload and re-validate the research a round-1 reassessment used.

    The research must still exist, match the batch and the answers that reassessment
    recorded, and be the same artifact (ID and sources) the reassessment named.
    Otherwise the follow-up is refused, so cited evidence is never detached.
    """
    named = Path(link.research_file)
    candidates = [named, reassessment_path.parent / named.name]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        raise ClarificationInputError(
            f"The reassessment used research {link.research_id}, but its file was not found "
            f"(tried {named}). A follow-up cannot carry that evidence. No call was made."
        )
    try:
        evidence = load_research_evidence(path, artifact, responses)
    except ClarificationInputError as exc:
        raise ClarificationInputError(
            f"The research this reassessment used cannot be validated: {exc}"
        ) from exc
    if evidence.research_id != link.research_id or sorted(evidence.source_ids) != sorted(
        link.source_ids
    ):
        raise ClarificationInputError(
            f"Research file {path} is not the research the reassessment used "
            f"({link.research_id}). No call was made."
        )
    return evidence


def _followup_input(source: FollowupSource) -> str:
    artifact = source.artifact
    context = artifact.context()
    assert source.reassessment.report is not None

    def dump(value: object) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False)

    questions = [
        {**q.model_dump(), "perspectives": question_perspectives(q, context.sources)}
        for q in artifact.questions
    ]
    return (
        f"{_questions_input(context)}\n\n"
        f"First-round clarification questions:\n{dump(questions)}\n\n"
        "First-round recorded responses (reported by the user; not independently verified):\n"
        f"{dump([_response_json(r) for r in source.responses.recorded])}\n\n"
        "First-round questions without a response (unanswered):\n"
        f"{dump(source.responses.unanswered)}\n\n"
        "Latest reassessment (a model interpretation of the findings and responses, not user "
        f"input):\n{dump(source.reassessment.report.model_dump())}"
        + (source.research.section() if source.research is not None else "")
    )


def _next_question_ids(existing: list[ClarificationQuestion], count: int) -> list[str]:
    """IDs that continue after the existing ones ("q4", "q5", ...), never reusing one."""
    taken = {q.id for q in existing}
    ids: list[str] = []
    n = len(existing) + 1
    while len(ids) < count:
        if f"q{n}" not in taken:
            ids.append(f"q{n}")
        n += 1
    return ids


async def prepare_followup(reassessment_path: Path) -> PreparedClarification:
    """Generate one follow-up batch (round 2) of up to three questions.

    Raises `ClarificationInputError` before any call, and before creating
    anything, if the lineage or references are invalid or a third round is
    attempted. Saves a new `clarify-*` directory next to the round-1 batch;
    earlier files are never modified. A failed call is saved as a
    `status: "failed"` artifact and returned.
    """
    source = load_followup_source(reassessment_path)
    get_anthropic_api_key()
    requested_model = get_anthropic_model()
    prior = source.artifact
    context = prior.context()
    assert source.reassessment.report is not None

    clarification_id, directory = create_run_dir(
        "clarify-", runs_dir=source.artifact_path.parent.parent
    )
    generation: dict[str, Any] = {
        "stage": "followup",
        "requested_model": requested_model,
        "system_prompt": FOLLOWUP_QUESTIONS_SYSTEM_PROMPT,
        "max_output_tokens": QUESTIONS_MAX_OUTPUT_TOKENS,
        "git": git_state(),
    }
    questions: list[ClarificationQuestion] = []
    call: CallDiagnostics | None = None
    error: ClarificationError | None = None
    try:
        batch, call = await _structured_call(
            stage="followup_questions",
            schema_name="question_batch",
            system=FOLLOWUP_QUESTIONS_SYSTEM_PROMPT,
            user_content=_followup_input(source),
            output_schema=QuestionBatch,
            max_tokens=QUESTIONS_MAX_OUTPUT_TOKENS,
            check=lambda parsed: _question_reference_issues(parsed, context.sources),
        )
        ids = _next_question_ids(prior.questions, len(batch.questions))
        questions = [
            ClarificationQuestion(id=qid, **q.model_dump())
            for qid, q in zip(ids, batch.questions, strict=True)
        ]
    except ClarificationError as exc:
        error = exc
        call = exc.call

    generation.update(
        status="failed" if error is not None else "success",
        **_call_fields(call, error),
        **_error_fields(error),
    )
    if error is None and not questions:
        # No useful follow-up: the preceding reassessment is the conclusion available for
        # this session. No other call is made and that file is not rewritten.
        generation["no_useful_followup"] = True
        generation["conclusion_reassessment_file"] = str(reassessment_path)
    artifact = ClarificationArtifact(
        clarification_id=clarification_id,
        created_at=utc_now(),
        status="failed" if error is not None else "success",
        source_run=prior.source_run,
        plan=prior.plan,
        experts=prior.experts,
        synthesis=prior.synthesis,
        sources=prior.sources,
        questions=questions,
        question_generation=generation,
        round=2,
        previous=ClarificationLineage(
            clarification_id=prior.clarification_id,
            clarification_file=str(source.artifact_path),
            reassessment_id=source.reassessment.reassessment_id,
            reassessment_file=str(reassessment_path),
        ),
        prior_questions=prior.questions,
        prior_responses=source.responses.recorded,
        prior_unanswered_question_ids=source.responses.unanswered,
        prior_reassessment=source.reassessment.report,
        prior_research=source.research.content if source.research is not None else None,
    )
    write_json(directory / ARTIFACT_FILENAME, artifact.to_json())
    if error is None:
        write_json(directory / TEMPLATE_FILENAME, _template(artifact, context.sources))
    return PreparedClarification(directory, artifact, call, error)


# --- CLI ---


def _print_stage_usage(label: str, usage: UsageSummary | None) -> None:
    if usage is None:
        return
    note = " (incomplete)" if usage.calls_missing_usage else ""
    print(
        f"{label} usage (this stage only): {usage.model_calls} call, "
        f"input tokens {usage.input_tokens}{note}, output tokens {usage.output_tokens}{note}",
        file=sys.stderr,
    )


def _print_error(error: ClarificationError) -> None:
    print(f"Error: {error}", file=sys.stderr)
    diagnostics = error.diagnostics
    if diagnostics is not None:
        print(
            f"Failure stage: {diagnostics.failure_stage}; stop reason: "
            f"{diagnostics.stop_reason or 'n/a'}",
            file=sys.stderr,
        )
        for issue in diagnostics.validation_errors or ():
            print(f"  {issue.schema}:{issue.loc} ({issue.type})", file=sys.stderr)
    call = error.call
    if call is None:
        return
    elapsed = f"{call.elapsed_seconds:.1f}s" if call.elapsed_seconds is not None else "n/a"
    print(
        f"Call: requested {call.requested_model}, max output tokens {call.max_output_tokens}, "
        f"elapsed {elapsed}",
        file=sys.stderr,
    )
    if not call.response_received:
        print("Response: none received", file=sys.stderr)
        return
    blocks = ", ".join(f"{kind} x{n}" for kind, n in (call.content_blocks or {}).items()) or "none"
    valid = {True: "yes", False: "no", None: "n/a (no text)"}[call.parse_text_is_valid_json]
    print(
        f"Response: model {call.returned_model}, stop reason {call.stop_reason or 'n/a'}, "
        f"blocks [{blocks}], text {call.text_characters} chars, valid JSON syntax: {valid}",
        file=sys.stderr,
    )


async def _run_prepare(run_dir: Path) -> int:
    try:
        prepared = await prepare(run_dir)
    except ClarificationInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return _report_prepared(prepared, "Question generation")


async def _run_followup(reassessment_path: Path) -> int:
    try:
        prepared = await prepare_followup(reassessment_path)
    except ClarificationInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    previous = prepared.artifact.previous
    if previous is not None:
        print(
            f"Follow-up round 2 for {previous.clarification_id} "
            f"(reassessment {previous.reassessment_id})",
            file=sys.stderr,
        )
    return _report_prepared(prepared, "Follow-up question generation")


# The bounded closing sentence, added only when no concerns or uncertainties remain.
NO_OPEN_CONCERNS_NOTE = (
    "Based on your answers, no additional open concerns remain among those examined."
)


def render_conclusion(report: ReassessmentReport | SavedReassessmentReport) -> str:
    """Render fields without another model call; keep legacy prose readable."""
    insight = report.takeaway
    lines: list[str] = []
    if isinstance(insight, ConclusionInsight):
        lines = [
            f"Blind spot: {insight.blind_spot}",
            f"Why it mattered: {insight.why_it_mattered}",
            f"Status: {insight.status.replace('_', ' ').capitalize()}. "
            f"{insight.status_explanation}",
        ]
    elif isinstance(insight, str):
        return f"Legacy takeaway: {insight}"
    elif isinstance(report, SavedReassessmentReport):
        lines.append("No takeaway was recorded; read the detailed sections.")
    if not report.remaining_considerations and not report.unresolved_uncertainties:
        lines.append(NO_OPEN_CONCERNS_NOTE)
    return "\n".join(lines)


def available_conclusion_lines(artifact: ClarificationArtifact) -> list[str]:
    """Lines pointing to the preceding reassessment as the session's available conclusion."""
    previous = artifact.previous
    report = artifact.prior_reassessment
    if previous is None or report is None:
        return []
    lines = [
        "The conclusion available for this session is the preceding reassessment:",
        f"  {previous.reassessment_file}",
        render_conclusion(report),
    ]
    open_items = len(report.remaining_considerations) + len(report.unresolved_uncertainties)
    if open_items:
        lines.append(
            f"  It still lists {len(report.remaining_considerations)} remaining "
            f"consideration(s) and {len(report.unresolved_uncertainties)} unresolved "
            "uncertainty(ies); stopping here does not resolve them."
        )
    else:
        lines.append("  It lists no remaining considerations or unresolved uncertainties.")
    return lines


def _print_available_conclusion(artifact: ClarificationArtifact) -> None:
    for line in available_conclusion_lines(artifact):
        print(line)


def _report_prepared(prepared: PreparedClarification, label: str) -> int:
    usage = prepared.call.usage if prepared.call else None
    if prepared.error is not None:
        _print_error(prepared.error)
        _print_stage_usage(label, usage)
        print(f"Failure saved in: {prepared.directory / ARTIFACT_FILENAME}", file=sys.stderr)
        return 1

    sources = prepared.artifact.context().sources
    questions = prepared.artifact.questions
    if not questions and prepared.artifact.round == 2:
        print(
            "No useful follow-up: the model found no open question whose answer could "
            "materially change the conclusion. This is not evidence that the plan is risk-free."
        )
        _print_available_conclusion(prepared.artifact)
    elif not questions:
        print("No clarification questions: the model judged that clarification would not help.")
    for q in questions:
        print(f"{q.id} [{', '.join(question_perspectives(q, sources))}]")
        print(f"  {q.question}")
        print(f"  Why it matters: {q.why_it_matters}")
        print(f"  Based on: {', '.join(q.source_finding_ids)}")
    _print_stage_usage(label, usage)
    print(f"Clarification saved in: {prepared.directory / ARTIFACT_FILENAME}", file=sys.stderr)
    print(f"Edit your responses in: {prepared.directory / TEMPLATE_FILENAME}", file=sys.stderr)
    return 0


async def _run_reassess(
    artifact_path: Path, responses_path: Path, research_path: Path | None = None
) -> int:
    try:
        result = await reassess(artifact_path, responses_path, research_path)
    except ClarificationInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if result.path is None:
        print(
            "No new context to reassess: every question is unanswered or deferred. "
            "No call was made and nothing was saved, so there is no new assessment.",
            file=sys.stderr,
        )
        if result.previous_reassessment_file is not None:
            print(
                f"The latest assessment is still: {result.previous_reassessment_file}",
                file=sys.stderr,
            )
        return 0
    usage = result.call.usage if result.call else None
    if result.error is not None:
        _print_error(result.error)
        _print_stage_usage("Reassessment", usage)
        print(f"Failure saved in: {result.path}", file=sys.stderr)
        print("No new assessment was produced.", file=sys.stderr)
        return 1
    assert result.report is not None
    print(json.dumps(result.report.model_dump(), indent=2, ensure_ascii=False))
    _print_stage_usage("Reassessment", usage)
    print(f"Reassessment saved in: {result.path}", file=sys.stderr)
    if result.round == 1:
        print(f"Current assessment (round 1): {render_conclusion(result.report)}", file=sys.stderr)
        print(
            "One follow-up round is available: uv run python -m blindspot.clarification "
            f"followup --reassessment {result.path}",
            file=sys.stderr,
        )
    else:
        print(
            f"Conclusion of this review (round 2): {render_conclusion(result.report)}",
            file=sys.stderr,
        )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.clarification",
        description="Clarification over a saved --auto-team run (one round plus one follow-up).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Generate up to three clarification questions.")
    prep.add_argument("--run", required=True, type=Path, help="A runs/team-* directory.")
    re = commands.add_parser("reassess", help="Reassess using a filled-in response file.")
    re.add_argument("--clarification", required=True, type=Path, help="clarification.json")
    re.add_argument("--responses", required=True, type=Path, help="Edited responses.json")
    re.add_argument(
        "--research",
        type=Path,
        help="Optional research-*.json made from this batch and these exact answers.",
    )
    follow = commands.add_parser(
        "followup", help="Generate one follow-up batch (round 2) from a successful reassessment."
    )
    follow.add_argument(
        "--reassessment", required=True, type=Path, help="A round-1 reassessment-*.json"
    )
    answer = commands.add_parser(
        "answer", help="Answer a batch's questions one at a time (no API key needed)."
    )
    answer.add_argument("--clarification", required=True, type=Path, help="clarification.json")
    answer.add_argument(
        "--responses",
        type=Path,
        help="Response file to read and update (default: responses.json next to the batch; "
        "created from the template if missing).",
    )
    answer.add_argument(
        "--revisit-deferred",
        action="store_true",
        help="Also ask questions previously saved for later.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "prepare":
        return asyncio.run(_run_prepare(args.run))
    if args.command == "followup":
        return asyncio.run(_run_followup(args.reassessment))
    if args.command == "answer":
        from blindspot.guided import run_guided_cli  # imports this module

        return run_guided_cli(args.clarification, args.responses, args.revisit_deferred)
    return asyncio.run(_run_reassess(args.clarification, args.responses, args.research))


if __name__ == "__main__":
    sys.exit(main())
