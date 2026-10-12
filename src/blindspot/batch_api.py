"""HTTP access to one saved clarification batch: view its questions and save answers.

A batch is addressed by its folder name (`clarify-…`), which the application created when
it saved the batch; clients never send filesystem paths. Lookups stay inside the
configured runs directory. Only `<runs>/<run>/<batch>/` and `<runs>/evals/<run>/<batch>/`
are searched, and a folder or file whose real path leaves the runs directory (for example
through a symlink) is refused.

Answers go to the batch's standard `responses.json`, the file the `answer` and `reassess`
commands use, through the same validation and atomic writes. In a follow-up (round 2)
batch, round-1 answers are shown but can't be changed.

Two endpoints call a model, each only after an explicit confirmation and never retried.
`POST …/reassessments` runs the existing `clarification.reassess` (one paid request).
While it runs, the batch's answers can't be changed through this API and no second
assessment of the batch can start, so the result always matches the answers it used.
`GET …/assessments/latest` only reads: it returns the batch's latest successful saved
assessment (never starting one), and says whether the answers have changed since.
`POST …/followups` runs the existing `clarification.prepare_followup` (one paid request)
from one named, current round-1 assessment, or returns a follow-up already generated
from it without another request.
"""

import asyncio
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from blindspot.clarification import (
    ARTIFACT_FILENAME,
    NO_OPEN_CONCERNS_NOTE,
    TEMPLATE_FILENAME,
    ClarificationArtifact,
    ClarificationError,
    ClarificationInputError,
    Reassessment,
    RecordedResponses,
    _artifact_path_for,
    _read_json,
    _reassessment_reference_issues,
    _SavedReassessment,
    _validate,
    load_artifact,
    load_followup_source,
    load_responses,
    prepare_followup,
    question_perspectives,
    reassess,
)
from blindspot.guided import open_responses
from blindspot.schemas import (
    ClarificationQuestion,
    ReassessmentReport,
    ResponseEntry,
    ResponseStatus,
    SavedReassessmentReport,
)

# Folder names made by `create_run_dir("clarify-")` (and the eval copies); no separators,
# dots, or glob characters, so a valid ID can't point anywhere else.
_BATCH_ID = re.compile(r"clarify-[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_LAYOUTS = ("*/{batch_id}", "evals/*/{batch_id}")
# Saved assessment files, as `reassess` names them (`reassessment-<reassessment_id>.json`).
_REASSESSMENT_FILE = re.compile(r"reassessment-([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.json")


# --- HTTP contract ---


class QuestionView(BaseModel):
    id: str
    round: Literal[1, 2]
    question: str
    why_it_matters: str
    perspectives: list[str]
    source_finding_ids: list[str]
    # None means unanswered, which is different from a deliberate "dont_know".
    status: ResponseStatus | None
    answer: str | None
    # False for round-1 questions shown in a follow-up batch: their answers are fixed.
    editable: bool


class BatchView(BaseModel):
    batch_id: str
    clarification_id: str
    round: Literal[1, 2]
    plan: str
    # Whether responses.json exists yet; it is created by the first saved answer.
    responses_saved: bool
    # The first question of this round with no response, for resuming; None when none is left.
    next_question_id: str | None
    # An assessment of this batch is in progress (answers can't change until it ends).
    assessment_running: bool
    # Follow-up questions are being generated from this batch (answers can't change either).
    followup_running: bool
    questions: list[QuestionView]


class ResponseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required (null is allowed, and clears the response back to unanswered).
    status: ResponseStatus | None
    answer: str | None = None


class AssessmentRequest(BaseModel):
    """Starting an assessment makes a paid model request, so the client must say so."""

    model_config = ConfigDict(extra="forbid")

    confirm_paid_request: Literal[True]


class AssessedResponse(BaseModel):
    question_id: str
    round: Literal[1, 2]
    status: ResponseStatus
    answer: str | None


class AssessmentView(BaseModel):
    batch_id: str
    reassessment_id: str
    round: Literal[1, 2]
    # "current_assessment" after round 1, "review_conclusion" after round 2 (as saved).
    conclusion_status: Literal["current_assessment", "review_conclusion"]
    report: ReassessmentReport
    # The bounded closing sentence, only when no concerns or uncertainties remain.
    closing_note: str | None
    # The answers the assessment used (both rounds in a follow-up), as saved in its file.
    assessed_responses: list[AssessedResponse]
    # The response file was edited outside this API while the request ran; the result
    # reflects `assessed_responses`, not the newer answers.
    answers_changed_during_assessment: bool
    # A follow-up batch's carried round-1 research was included.
    used_research: bool


class SavedAssessmentView(BaseModel):
    """A successful assessment read back from its saved file (no model call)."""

    batch_id: str
    reassessment_id: str
    # The file's `created_at` (UTC, ISO 8601), which decides which assessment is latest.
    created_at: str
    round: Literal[1, 2]
    conclusion_status: Literal["current_assessment", "review_conclusion"]
    # Saved reports may have a legacy prose takeaway, or none (older files).
    report: SavedReassessmentReport
    closing_note: str | None
    assessed_responses: list[AssessedResponse]
    # The batch's saved answers now differ from `assessed_responses` (compared by question,
    # status, and answer; file times are never used).
    based_on_earlier_answers: bool
    used_research: bool


class LatestAssessment(BaseModel):
    # None when the batch has no usable successful assessment (failed attempts don't count).
    assessment: SavedAssessmentView | None
    # Saved assessment files that were skipped because they can't be used: unreadable,
    # invalid, an unsupported format, another batch's, or a link. Failed attempts aren't
    # counted.
    unusable_files: int


class FollowupRequest(BaseModel):
    """Follow-up questions come from one named assessment and cost a paid request."""

    model_config = ConfigDict(extra="forbid")

    reassessment_id: str
    confirm_paid_request: Literal[True]


class FollowupView(BaseModel):
    batch_id: str
    reassessment_id: str
    # The round-2 batch (open it like any batch). With no questions it is the saved record
    # of "no useful follow-up", and the assessment remains the available conclusion.
    followup_batch_id: str
    question_count: int
    no_useful_followup: bool
    # The assessment still lists remaining concerns or unresolved uncertainties.
    open_concerns: bool
    # An earlier successful follow-up from this assessment was returned; no request made.
    reused: bool
    # The round-1 response file was edited outside this API while generating. The
    # follow-up carries the answers the assessment used, not the newer ones.
    answers_changed_during_generation: bool


# --- Locating and reading a batch ---

RunningKind = Literal["assessment", "followup"]


@dataclass
class BatchStore:
    """The configured runs directory.

    `lock` serializes response-file writes and the bookkeeping of `running`: the batch
    folders with a paid request in progress (an assessment, or follow-up generation from
    a round-1 batch), at most one per batch across all tabs and clients of this server
    process.
    """

    runs_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: dict[Path, RunningKind] = field(default_factory=dict)

    def running_kind(self, directory: Path) -> RunningKind | None:
        return self.running.get(directory.resolve())

    def finish(self, directory: Path) -> None:
        with self.lock:
            self.running.pop(directory.resolve(), None)

    def locate(self, batch_id: str) -> Path:
        """Return the batch folder, or raise 404 / 409 / 403."""
        if not _BATCH_ID.fullmatch(batch_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Clarification batch not found.")
        found = sorted(
            {
                path
                for layout in _LAYOUTS
                for path in self.runs_dir.glob(layout.format(batch_id=batch_id))
                if (path / ARTIFACT_FILENAME).is_file()
            }
        )
        if not found:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Clarification batch not found.")
        if len(found) > 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"More than one saved batch is named {batch_id}; none was used.",
            )
        directory = found[0]
        root = self.runs_dir.resolve()
        for path in (directory, directory / ARTIFACT_FILENAME):
            if not path.resolve().is_relative_to(root):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "This batch resolves outside the runs directory; it was not used.",
                )
        # The response file is the one file written here: it must be a real file, not a
        # link to anything (inside the runs directory or not). Saves go through a fresh,
        # uniquely named temporary file (`write_json`), so no other name is trusted.
        if (directory / TEMPLATE_FILENAME).is_symlink():
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This batch's responses.json is a symbolic link; it was not used.",
            )
        return directory


def _store(request: Request) -> BatchStore:
    store: BatchStore = request.app.state.batch_store
    return store


Store = Annotated[BatchStore, Depends(_store)]


def _load_artifact(directory: Path) -> ClarificationArtifact:
    try:
        return load_artifact(directory / ARTIFACT_FILENAME)
    except ClarificationInputError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"The saved batch can't be used: {exc}"
        ) from None


def _invalid_responses(exc: ClarificationInputError) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, f"{exc}. The response file was not changed; fix it first."
    )


def _view(
    batch_id: str, directory: Path, artifact: ClarificationArtifact, running: RunningKind | None
) -> BatchView:
    path = directory / TEMPLATE_FILENAME
    current: dict[str, ResponseEntry] = {}
    if path.exists():
        try:
            current = {r.question_id: r for r in load_responses(path, artifact).recorded}
        except ClarificationInputError as exc:
            raise _invalid_responses(exc) from None
    prior = {r.question_id: r for r in artifact.prior_responses}
    sources = artifact.context().sources

    def view(q: ClarificationQuestion, entry: ResponseEntry | None, editable: bool) -> QuestionView:
        return QuestionView(
            id=q.id,
            round=artifact.round if editable else 1,
            question=q.question,
            why_it_matters=q.why_it_matters,
            perspectives=question_perspectives(q, sources),
            source_finding_ids=q.source_finding_ids,
            status=entry.status if entry is not None else None,
            answer=entry.answer if entry is not None else None,
            editable=editable,
        )

    views = [view(q, prior.get(q.id), editable=False) for q in artifact.prior_questions]
    views += [view(q, current.get(q.id), editable=True) for q in artifact.questions]
    return BatchView(
        batch_id=batch_id,
        clarification_id=artifact.clarification_id,
        round=artifact.round,
        plan=artifact.plan,
        responses_saved=path.exists(),
        next_question_id=next((q.id for q in artifact.questions if q.id not in current), None),
        assessment_running=running == "assessment",
        followup_running=running == "followup",
        questions=views,
    )


# --- Endpoints ---

router = APIRouter(prefix="/clarifications", tags=["clarification"])


@router.get("/{batch_id}")
def get_batch(batch_id: str, store: Store) -> BatchView:
    """The batch's questions, perspectives, and current response statuses."""
    directory = store.locate(batch_id)
    return _view(batch_id, directory, _load_artifact(directory), store.running_kind(directory))


@router.put("/{batch_id}/responses/{question_id}")
def put_response(
    batch_id: str, question_id: str, update: ResponseUpdate, store: Store
) -> BatchView:
    """Save or replace one question's response; every other entry is kept as it was."""
    with store.lock:
        directory = store.locate(batch_id)
        running = store.running_kind(directory)
        if running is not None:
            what = "An assessment of" if running == "assessment" else "Follow-up generation from"
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{what} this batch is running, so answers can't change until it finishes. "
                "Nothing was saved; try again afterwards.",
            )
        artifact = _load_artifact(directory)
        if any(q.id == question_id for q in artifact.prior_questions):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{question_id} belongs to round 1; earlier answers can't be changed.",
            )
        if not any(q.id == question_id for q in artifact.questions):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "This batch has no question with that ID."
            )
        try:
            ResponseEntry(question_id=question_id, status=update.status, answer=update.answer)
        except pydantic.ValidationError as exc:
            reasons = "; ".join(str(e["msg"]) for e in exc.errors())
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, reasons) from None
        try:
            book, _ = open_responses(artifact, directory / TEMPLATE_FILENAME)
        except ClarificationInputError as exc:
            raise _invalid_responses(exc) from None
        book.record(question_id, update.status, update.answer)
        return _view(batch_id, directory, artifact, running=None)


# --- Assessment (one paid model request) ---


def _snapshot(responses: RecordedResponses) -> list[tuple[str, Any, Any]]:
    return [(r.question_id, r.status, r.answer) for r in responses.recorded]


def _already_running(kind: RunningKind) -> HTTPException:
    what = "An assessment of" if kind == "assessment" else "Follow-up generation from"
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"{what} this batch is already running (perhaps from another tab). "
        "No new request was made.",
    )


def _failure_message(
    error: ClarificationError,
    path: Path | None,
    produced: str = "an assessment",
    kind: str = "assessment",
    outcome: str = "No new assessment was produced",
) -> str:
    """A content-free explanation built from diagnostics, never from model output."""
    call = error.call
    diagnostics = error.diagnostics
    if call is None or not call.response_received:
        reason = (
            "The model request didn't complete (the service couldn't be reached, timed out, "
            "or returned an error)."
        )
    elif call.stop_reason == "max_tokens":
        reason = "The model's response was cut off before it finished."
    elif call.stop_reason == "refusal":
        reason = f"The model declined to produce {produced}."
    elif diagnostics is not None and diagnostics.validation_errors:
        reason = f"The model's response didn't match the expected {kind} format."
    else:
        reason = "The model's response couldn't be used."
    saved = f" The failed attempt is recorded in {path.name}." if path is not None else ""
    return f"{reason} {outcome}, and nothing was retried. Your answers are still saved.{saved}"


def _conclusion_status(
    artifact: ClarificationArtifact,
) -> Literal["current_assessment", "review_conclusion"]:
    return "current_assessment" if artifact.round == 1 else "review_conclusion"


def _closing_note(report: ReassessmentReport | SavedReassessmentReport) -> str | None:
    open_items = report.remaining_considerations or report.unresolved_uncertainties
    return None if open_items else NO_OPEN_CONCERNS_NOTE


def _assessed_responses(
    artifact: ClarificationArtifact, used: list[ResponseEntry]
) -> list[AssessedResponse]:
    earlier = {q.id for q in artifact.prior_questions}
    return [
        AssessedResponse(
            question_id=r.question_id,
            round=1 if r.question_id in earlier else artifact.round,
            status=r.status,
            answer=r.answer,
        )
        for r in used
        if r.status is not None
    ]


def _assessment_view(
    batch_id: str, artifact: ClarificationArtifact, result: Reassessment, changed: bool
) -> AssessmentView:
    assert result.path is not None and result.report is not None
    return AssessmentView(
        batch_id=batch_id,
        reassessment_id=result.path.stem.removeprefix("reassessment-"),
        round=artifact.round,
        conclusion_status=_conclusion_status(artifact),
        report=result.report,
        closing_note=_closing_note(result.report),
        assessed_responses=_assessed_responses(
            artifact, [*artifact.prior_responses, *result.responses.recorded]
        ),
        answers_changed_during_assessment=changed,
        used_research=artifact.prior_research is not None,
    )


@router.post("/{batch_id}/reassessments")
async def post_reassessment(
    batch_id: str, request: AssessmentRequest, store: Store
) -> AssessmentView:
    """Assess this round's saved answers: one paid model request, never retried.

    Refused before any request when the batch or response file is invalid, nothing usable
    is answered (every question unanswered or saved for later), the server has no model
    configuration, or an assessment of this batch is already running.
    """
    with store.lock:
        directory = store.locate(batch_id)
        key = directory.resolve()
        if key in store.running:
            raise _already_running(store.running[key])
        artifact = _load_artifact(directory)
        responses_path = directory / TEMPLATE_FILENAME
        try:
            before = load_responses(responses_path, artifact) if responses_path.exists() else None
        except ClarificationInputError as exc:
            raise _invalid_responses(exc) from None
        if before is None or not before.has_new_context:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Nothing to assess yet: every question in this round is unanswered or saved "
                'for later. Answer at least one ("I don\'t know" and "Not applicable" count). '
                "No request was made.",
            )
        store.running[key] = "assessment"

    # The batch stays marked as running until the call and its saved file are complete,
    # even if the client disconnects: the shielded task isn't cancelled with the request.
    task = asyncio.create_task(reassess(directory / ARTIFACT_FILENAME, responses_path))
    task.add_done_callback(lambda _: store.finish(directory))
    try:
        result = await asyncio.shield(task)
    except ClarificationInputError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{exc}. No request was made.") from None
    except ValueError:  # missing ANTHROPIC_API_KEY / ANTHROPIC_MODEL, checked before any call
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The server isn't configured for model requests (see Configuration in the "
            "README). No request was made.",
        ) from None

    if result.path is None:  # nothing new after all; reassess made no call
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing to assess yet. No request was made.")
    if result.error is not None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, _failure_message(result.error, result.path)
        )
    # Answers can't change through this API while the call runs, but the file could be
    # edited by hand or by the CLI. Report that rather than implying the newer answers
    # were assessed.
    try:
        changed = _snapshot(load_responses(responses_path, artifact)) != _snapshot(result.responses)
    except ClarificationInputError:
        changed = True
    return _assessment_view(batch_id, artifact, result, changed)


# --- Reopening the latest saved assessment (no model call) ---


class _SavedAssessmentFile(_SavedReassessment):
    """A saved `reassessment-*.json`, with the fields needed to choose and show it."""

    created_at: str
    # Absent in files saved before it was added.
    conclusion_status: str | None = None


@dataclass(frozen=True)
class _SavedAssessment:
    saved: _SavedAssessmentFile
    created_at: datetime
    report: SavedReassessmentReport
    used: list[ResponseEntry]


def _read_saved_assessment(
    path: Path, artifact: ClarificationArtifact, root: Path
) -> _SavedAssessment | None:
    """A successful assessment of this batch, or None for a failed attempt.

    Raises `ClarificationInputError` for a file that can't be used. Checks that it belongs
    to this batch and round, that its recorded answers are valid responses to this batch's
    questions (in a follow-up, with round 1's answers as the batch carries them), and that
    the report's finding and question citations resolve as they must for a follow-up.
    Research citations are checked only against the file's own `research.source_ids`
    link; the research file isn't reloaded.
    """
    match = _REASSESSMENT_FILE.fullmatch(path.name)
    if match is None or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ClarificationInputError("not a saved assessment file inside the runs directory")
    saved: _SavedAssessmentFile = _validate(
        _SavedAssessmentFile, _read_json(path, "Reassessment"), "Reassessment"
    )
    if saved.reassessment_id != match.group(1):
        raise ClarificationInputError("reassessment ID doesn't match the file name")
    if saved.clarification_id != artifact.clarification_id:
        raise ClarificationInputError("belongs to another batch")
    if saved.clarification_round != artifact.round:
        raise ClarificationInputError("belongs to another round")
    if saved.status == "failed":
        return None
    if saved.report is None:
        raise ClarificationInputError("successful assessment without a report")
    try:
        created_at = datetime.fromisoformat(saved.created_at)
    except ValueError:
        raise ClarificationInputError("creation time isn't an ISO 8601 timestamp") from None
    if created_at.tzinfo is None:
        raise ClarificationInputError("creation time has no time zone")
    if saved.conclusion_status not in (None, _conclusion_status(artifact)):
        raise ClarificationInputError("conclusion status doesn't match the batch's round")
    try:
        used = [
            ResponseEntry(question_id=r.question_id, status=r.status, answer=r.answer)
            for r in saved.recorded_responses
        ]
    except pydantic.ValidationError:
        raise ClarificationInputError("recorded responses are invalid") from None
    ids = [r.question_id for r in used]
    all_ids = {q.id for q in artifact.all_questions}
    if len(set(ids)) != len(ids) or not set(ids) <= all_ids:
        raise ClarificationInputError("recorded responses don't match the batch's questions")
    earlier = {q.id for q in artifact.prior_questions}
    if _entries([r for r in used if r.question_id in earlier]) != _entries(
        artifact.prior_responses
    ):
        raise ClarificationInputError("round-1 answers differ from those the batch carries")
    research = set(saved.research.source_ids) if saved.research is not None else None
    responses = RecordedResponses(used, list(saved.unanswered_question_ids))
    if _reassessment_reference_issues(saved.report, artifact, responses, research):
        raise ClarificationInputError(
            "report cites findings, questions, or sources that don't resolve"
        )
    return _SavedAssessment(saved, created_at, saved.report, used)


def _entries(entries: list[ResponseEntry]) -> list[tuple[str, Any, Any]]:
    return sorted((r.question_id, r.status, r.answer) for r in entries)


def _latest_saved_assessment(
    directory: Path, artifact: ClarificationArtifact, root: Path
) -> tuple[_SavedAssessment | None, int]:
    """The latest successful assessment by saved `created_at` (ties: reassessment ID), and
    how many files were skipped as unusable. Failed attempts are skipped silently, so a
    newer failure never hides an older success."""
    found: list[_SavedAssessment] = []
    unusable = 0
    for path in sorted(directory.glob("reassessment-*.json")):
        try:
            assessment = _read_saved_assessment(path, artifact, root)
        except ClarificationInputError:
            unusable += 1
            continue
        if assessment is not None:
            found.append(assessment)
    latest = max(
        found,
        key=lambda a: (a.created_at, a.saved.reassessment_id),
        default=None,
    )
    return latest, unusable


@router.get("/{batch_id}/assessments/latest")
def get_latest_assessment(batch_id: str, store: Store) -> LatestAssessment:
    """The batch's latest successful saved assessment, read from its file. Never calls a
    model or starts an assessment; failed attempts and unusable files are skipped."""
    directory = store.locate(batch_id)
    artifact = _load_artifact(directory)
    latest, unusable = _latest_saved_assessment(directory, artifact, store.runs_dir.resolve())
    if latest is None:
        return LatestAssessment(assessment=None, unusable_files=unusable)
    responses_path = directory / TEMPLATE_FILENAME
    try:
        current = (
            load_responses(responses_path, artifact).recorded if responses_path.exists() else []
        )
    except ClarificationInputError as exc:
        raise _invalid_responses(exc) from None
    saved = latest.saved
    return LatestAssessment(
        assessment=SavedAssessmentView(
            batch_id=batch_id,
            reassessment_id=saved.reassessment_id,
            created_at=latest.created_at.isoformat(),
            round=artifact.round,
            conclusion_status=_conclusion_status(artifact),
            report=latest.report,
            closing_note=_closing_note(latest.report),
            assessed_responses=_assessed_responses(artifact, latest.used),
            based_on_earlier_answers=_entries(latest.used)
            != _entries([*artifact.prior_responses, *current]),
            used_research=saved.research is not None,
        ),
        unusable_files=unusable,
    )


# --- Follow-up questions (one paid model request) ---

_NO_FOLLOWUP = "No request was made."


def _followup_refused(message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, f"{message} {_NO_FOLLOWUP}")


def _current_entries(
    directory: Path, artifact: ClarificationArtifact
) -> list[tuple[str, Any, Any]]:
    path = directory / TEMPLATE_FILENAME
    try:
        current = load_responses(path, artifact).recorded if path.exists() else []
    except ClarificationInputError as exc:
        raise _invalid_responses(exc) from None
    return _entries([*artifact.prior_responses, *current])


def _named_assessment(
    directory: Path, artifact: ClarificationArtifact, reassessment_id: str, root: Path
) -> tuple[Path, _SavedAssessment]:
    """The batch's own successful assessment with this ID, validated like a reopened one."""
    name = f"reassessment-{reassessment_id}.json"
    # The ID is checked before it names a file, so it can't point anywhere else.
    path = directory / name
    if not _REASSESSMENT_FILE.fullmatch(name) or not path.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "This batch has no saved assessment with that ID."
        )
    try:
        assessment = _read_saved_assessment(path, artifact, root)
    except ClarificationInputError:
        raise _followup_refused(
            "That saved assessment can't be used: it is invalid, an unsupported format, or "
            "doesn't belong to this batch."
        ) from None
    if assessment is None:
        raise _followup_refused(
            "That assessment failed, so it can't start a follow-up. Assess your answers first."
        )
    return path, assessment


def _check_followup_lineage(
    path: Path, assessment: _SavedAssessment, directory: Path, root: Path
) -> None:
    """Everything `prepare_followup` will read must be this batch's own files inside the runs
    directory: the artifact it resolves, and any research the assessment used."""
    try:
        source = load_followup_source(path)
    except ClarificationInputError:
        raise _followup_refused(
            "This assessment can't start a follow-up: its saved answers, references, or "
            "research don't pass the lineage checks."
        ) from None
    expected = (directory / ARTIFACT_FILENAME).resolve()
    if _artifact_path_for(path, assessment.saved).resolve() != expected or (
        source.artifact_path.resolve() != expected
    ):
        raise _followup_refused(
            "This assessment names a clarification file other than its batch's, so it wasn't used."
        )
    link = assessment.saved.research
    if link is not None:
        named = Path(link.research_file)
        used = next((c for c in (named, path.parent / named.name) if c.is_file()), None)
        if used is None or used.is_symlink() or not used.resolve().is_relative_to(root):
            raise _followup_refused(
                "The research this assessment used isn't a file inside the runs directory, "
                "so it wasn't used."
            )


def _existing_followup(
    directory: Path, artifact: ClarificationArtifact, reassessment_id: str, root: Path
) -> tuple[str, ClarificationArtifact] | None:
    """The latest successful follow-up already generated from this assessment, if any.
    Failed attempts and unreadable folders are ignored, so a failure can be retried."""
    found: list[tuple[datetime, str, ClarificationArtifact]] = []
    for candidate in directory.parent.glob("clarify-*"):
        file = candidate / ARTIFACT_FILENAME
        if (
            candidate == directory
            or not _BATCH_ID.fullmatch(candidate.name)
            or candidate.is_symlink()
            or file.is_symlink()
            or not file.is_file()
            or not file.resolve().is_relative_to(root)
        ):
            continue
        try:
            followup = load_artifact(file)  # rejects failed attempts
            created = datetime.fromisoformat(followup.created_at)
        except (ClarificationInputError, ValueError):
            continue
        previous = followup.previous
        if (
            followup.round == 2
            and previous is not None
            and previous.clarification_id == artifact.clarification_id
            and previous.reassessment_id == reassessment_id
            and created.tzinfo is not None
        ):
            found.append((created, candidate.name, followup))
    if not found:
        return None
    _, name, followup = max(found, key=lambda f: (f[0], f[1]))
    return name, followup


def _followup_view(
    batch_id: str,
    reassessment_id: str,
    followup_id: str,
    followup: ClarificationArtifact,
    assessment: _SavedAssessment,
    *,
    reused: bool,
    changed: bool,
) -> FollowupView:
    report = assessment.report
    return FollowupView(
        batch_id=batch_id,
        reassessment_id=reassessment_id,
        followup_batch_id=followup_id,
        question_count=len(followup.questions),
        no_useful_followup=not followup.questions,
        open_concerns=bool(report.remaining_considerations or report.unresolved_uncertainties),
        reused=reused,
        answers_changed_during_generation=changed,
    )


@router.post("/{batch_id}/followups")
async def post_followup(batch_id: str, request: FollowupRequest, store: Store) -> FollowupView:
    """Generate follow-up questions (round 2) from one named round-1 assessment.

    One paid model request, never retried. Refused before any request when the batch isn't
    round 1, the assessment isn't this batch's successful one, it lists nothing open, the
    batch's saved answers changed since it (assess again first), its lineage or research
    don't pass the follow-up checks, the server has no model configuration, or a paid
    request for this batch is already running. A successful follow-up already generated
    from the same assessment is returned instead, without a request.
    """
    root = store.runs_dir.resolve()
    with store.lock:
        directory = store.locate(batch_id)
        key = directory.resolve()
        if key in store.running:
            raise _already_running(store.running[key])
        artifact = _load_artifact(directory)
        if artifact.round != 1:
            raise _followup_refused(
                "This is already a follow-up batch; Blindspot supports only one follow-up round."
            )
        path, assessment = _named_assessment(directory, artifact, request.reassessment_id, root)
        report = assessment.report
        if not (report.remaining_considerations or report.unresolved_uncertainties):
            raise _followup_refused(
                "This assessment lists no remaining concerns or unresolved uncertainties to "
                "follow up on."
            )
        assessed = _entries(assessment.used)
        if _current_entries(directory, artifact) != assessed:
            raise _followup_refused(
                "Your saved answers changed after this assessment. Assess your answers again "
                "first, so follow-up questions don't rely on earlier answers."
            )
        existing = _existing_followup(directory, artifact, request.reassessment_id, root)
        if existing is not None:
            name, followup = existing
            return _followup_view(
                batch_id, request.reassessment_id, name, followup, assessment,
                reused=True, changed=False,
            )  # fmt: skip
        _check_followup_lineage(path, assessment, directory, root)
        store.running[key] = "followup"

    # As with assessment: the batch stays marked as running (so its answers can't change
    # and no second request starts) until the call and its saved files are complete, even
    # if the client disconnects.
    task = asyncio.create_task(prepare_followup(path))
    task.add_done_callback(lambda _: store.finish(directory))
    try:
        prepared = await asyncio.shield(task)
    except ClarificationInputError:
        raise _followup_refused(
            "This assessment can't start a follow-up: its saved files don't pass the lineage "
            "checks."
        ) from None
    except ValueError:  # missing ANTHROPIC_API_KEY / ANTHROPIC_MODEL, checked before any call
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The server isn't configured for model requests (see Configuration in the "
            f"README). {_NO_FOLLOWUP}",
        ) from None
    if prepared.error is not None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            _failure_message(
                prepared.error,
                prepared.directory,
                produced="follow-up questions",
                kind="question",
                outcome="No follow-up questions were produced",
            ),
        )
    # Answers can't change through this API meanwhile, but the file could be edited by hand
    # or by the CLI; the follow-up carries the assessed answers either way.
    try:
        changed = _current_entries(directory, artifact) != assessed
    except HTTPException:
        changed = True
    return _followup_view(
        batch_id, request.reassessment_id, prepared.directory.name, prepared.artifact,
        assessment, reused=False, changed=changed,
    )  # fmt: skip
