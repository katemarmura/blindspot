"""One clarification round over a saved `--auto-team` run: questions, then reassessment.

Run with:
    uv run python -m blindspot.clarification prepare --run runs/team-<id>
    # edit runs/team-<id>/clarify-<id>/responses.json
    uv run python -m blindspot.clarification reassess \\
        --clarification runs/team-<id>/clarify-<id>/clarification.json \\
        --responses runs/team-<id>/clarify-<id>/responses.json

`prepare` loads the run's `result.json`, makes one structured-output call for
up to three questions (citing original finding IDs), and writes a new
`clarify-*` directory with `clarification.json` and an editable
`responses.json` template. `reassess` validates both files and makes one call
that reassesses the original findings in light of the recorded responses,
saved as a new `reassessment-*.json`. Each command makes at most one model
call: no recovery, retries, or repair. Original run files are never modified,
and experts are never rerun. This is one round, not an automatic loop.
"""

import argparse
import asyncio
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
from pydantic import BaseModel, ConfigDict, model_validator

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
from blindspot.prompts import CLARIFICATION_QUESTIONS_SYSTEM_PROMPT, REASSESSMENT_SYSTEM_PROMPT
from blindspot.runs import create_run_dir, git_state, utc_now, write_json
from blindspot.schemas import (
    ClarificationQuestion,
    ExpertDefinition,
    ExpertReport,
    QuestionBatch,
    ReassessmentReport,
    ResponseEntry,
    ResponseFile,
    ResponseStatus,
    SynthesisReport,
)
from blindspot.synthesis import ExpertInput, SourceRef, build_input, source_map

QUESTIONS_MAX_OUTPUT_TOKENS = 1500
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
    except (OSError, json.JSONDecodeError) as exc:
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
    label = "Question generation" if stage == "clarification_questions" else "Reassessment"
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


class _ArtifactExpert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    assignment: ExpertDefinition
    report: ExpertReport | None


class ClarificationArtifact(BaseModel):
    """`clarification.json`: the original context, questions, and call metadata.

    Holds everything reassessment needs, so it doesn't depend on the run's
    files. Source IDs are re-derived from `experts` when loaded; the saved
    `sources` are for inspection.
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

    @model_validator(mode="after")
    def _check_unique_question_ids(self) -> "ClarificationArtifact":
        # Responses are matched to questions by ID, so a duplicate would attach
        # one answer to more than one question.
        ids = [q.id for q in self.questions]
        duplicates = sorted({qid for qid in ids if ids.count(qid) > 1})
        if duplicates:
            raise ValueError(f"duplicate question IDs: {', '.join(duplicates)}")
        return self

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
    write_json(directory / ARTIFACT_FILENAME, artifact.model_dump(mode="json"))
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
    for q in artifact.questions:
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


def _reassessment_input(artifact: ClarificationArtifact, responses: RecordedResponses) -> str:
    context = artifact.context()

    def dump(value: object) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False)

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
    report: ReassessmentReport, artifact: ClarificationArtifact, responses: RecordedResponses
) -> tuple[ValidationIssue, ...]:
    sources = artifact.context().sources
    question_ids = {q.id for q in artifact.questions}
    issues: list[ValidationIssue] = []

    def issue(loc: str, kind: str) -> None:
        issues.append(ValidationIssue(schema="reassessment_report", loc=loc, type=kind))

    sections: list[tuple[str, list[Any]]] = [
        ("remaining_considerations", report.remaining_considerations),
        ("addressed_concerns", report.addressed_concerns),
        ("unresolved_uncertainties", report.unresolved_uncertainties),
    ]
    for section, items in sections:
        for i, item in enumerate(items):
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


@dataclass(frozen=True)
class Reassessment:
    path: Path | None
    report: ReassessmentReport | None
    call: CallDiagnostics | None
    error: ClarificationError | None
    responses: RecordedResponses


async def reassess(artifact_path: Path, responses_path: Path) -> Reassessment:
    """Reassess once with the recorded responses; save to a new `reassessment-*.json`.

    Raises `ClarificationInputError` for invalid files. Makes no call (and
    saves nothing) when every response is missing or deferred.
    """
    artifact = load_artifact(artifact_path)
    responses = load_responses(responses_path, artifact)
    if not responses.has_new_context:
        return Reassessment(None, None, None, None, responses)
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
            user_content=_reassessment_input(artifact, responses),
            output_schema=ReassessmentReport,
            max_tokens=REASSESSMENT_MAX_OUTPUT_TOKENS,
            check=lambda parsed: _reassessment_reference_issues(parsed, artifact, responses),
        )
    except ClarificationError as exc:
        error = exc
        call = exc.call

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
            "recorded_responses": [_response_json(r) for r in responses.recorded],
            "unanswered_question_ids": responses.unanswered,
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
    return Reassessment(path, report, call, error, responses)


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
    usage = prepared.call.usage if prepared.call else None
    if prepared.error is not None:
        _print_error(prepared.error)
        _print_stage_usage("Question generation", usage)
        print(f"Failure saved in: {prepared.directory / ARTIFACT_FILENAME}", file=sys.stderr)
        return 1

    sources = prepared.artifact.context().sources
    questions = prepared.artifact.questions
    if not questions:
        print("No clarification questions: the model judged that clarification would not help.")
    for q in questions:
        print(f"{q.id} [{', '.join(question_perspectives(q, sources))}]")
        print(f"  {q.question}")
        print(f"  Why it matters: {q.why_it_matters}")
        print(f"  Based on: {', '.join(q.source_finding_ids)}")
    _print_stage_usage("Question generation", usage)
    print(f"Clarification saved in: {prepared.directory / ARTIFACT_FILENAME}", file=sys.stderr)
    print(f"Edit your responses in: {prepared.directory / TEMPLATE_FILENAME}", file=sys.stderr)
    return 0


async def _run_reassess(artifact_path: Path, responses_path: Path) -> int:
    try:
        result = await reassess(artifact_path, responses_path)
    except ClarificationInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if result.path is None:
        print(
            "No new context to reassess: every question is unanswered or deferred. "
            "No call was made and nothing was saved.",
            file=sys.stderr,
        )
        return 0
    usage = result.call.usage if result.call else None
    if result.error is not None:
        _print_error(result.error)
        _print_stage_usage("Reassessment", usage)
        print(f"Failure saved in: {result.path}", file=sys.stderr)
        return 1
    assert result.report is not None
    print(json.dumps(result.report.model_dump(), indent=2, ensure_ascii=False))
    _print_stage_usage("Reassessment", usage)
    print(f"Reassessment saved in: {result.path}", file=sys.stderr)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.clarification",
        description="One clarification round over a saved --auto-team run.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Generate up to three clarification questions.")
    prep.add_argument("--run", required=True, type=Path, help="A runs/team-* directory.")
    re = commands.add_parser("reassess", help="Reassess using a filled-in response file.")
    re.add_argument("--clarification", required=True, type=Path, help="clarification.json")
    re.add_argument("--responses", required=True, type=Path, help="Edited responses.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "prepare":
        return asyncio.run(_run_prepare(args.run))
    return asyncio.run(_run_reassess(args.clarification, args.responses))


if __name__ == "__main__":
    sys.exit(main())
