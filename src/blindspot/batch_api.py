"""HTTP access to one saved clarification batch: view its questions and save answers.

A batch is addressed by its folder name (`clarify-…`), which the application created when
it saved the batch; clients never send filesystem paths. Lookups stay inside the
configured runs directory. Only `<runs>/<run>/<batch>/` and `<runs>/evals/<run>/<batch>/`
are searched, and a folder or file whose real path leaves the runs directory (for example
through a symlink) is refused.

Answers go to the batch's standard `responses.json`, the file the `answer` and `reassess`
commands use, through the same validation and atomic writes. In a follow-up (round 2)
batch, round-1 answers are shown but can't be changed.

Only one endpoint calls a model: `POST …/reassessments`, which runs the existing
`clarification.reassess` (one paid request, no retries) after an explicit confirmation.
While it runs, the batch's answers can't be changed through this API and no second
assessment of the batch can start, so the result always matches the answers it used.
"""

import asyncio
import re
import threading
from dataclasses import dataclass, field
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
    load_artifact,
    load_responses,
    question_perspectives,
    reassess,
)
from blindspot.guided import open_responses
from blindspot.schemas import (
    ClarificationQuestion,
    ReassessmentReport,
    ResponseEntry,
    ResponseStatus,
)

# Folder names made by `create_run_dir("clarify-")` (and the eval copies); no separators,
# dots, or glob characters, so a valid ID can't point anywhere else.
_BATCH_ID = re.compile(r"clarify-[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_LAYOUTS = ("*/{batch_id}", "evals/*/{batch_id}")


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


# --- Locating and reading a batch ---


@dataclass
class BatchStore:
    """The configured runs directory.

    `lock` serializes response-file writes and the bookkeeping of `running`, the batch
    folders with an assessment in progress (one per batch, across all tabs and clients of
    this server process).
    """

    runs_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: set[Path] = field(default_factory=set)

    def is_running(self, directory: Path) -> bool:
        return directory.resolve() in self.running

    def finish(self, directory: Path) -> None:
        with self.lock:
            self.running.discard(directory.resolve())

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
    batch_id: str, directory: Path, artifact: ClarificationArtifact, store_running: bool
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
        assessment_running=store_running,
        questions=views,
    )


# --- Endpoints ---

router = APIRouter(prefix="/clarifications", tags=["clarification"])


@router.get("/{batch_id}")
def get_batch(batch_id: str, store: Store) -> BatchView:
    """The batch's questions, perspectives, and current response statuses."""
    directory = store.locate(batch_id)
    return _view(batch_id, directory, _load_artifact(directory), store.is_running(directory))


@router.put("/{batch_id}/responses/{question_id}")
def put_response(
    batch_id: str, question_id: str, update: ResponseUpdate, store: Store
) -> BatchView:
    """Save or replace one question's response; every other entry is kept as it was."""
    with store.lock:
        directory = store.locate(batch_id)
        if directory.resolve() in store.running:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An assessment of this batch is running, so answers can't change until it "
                "finishes. Nothing was saved; try again afterwards.",
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
        return _view(batch_id, directory, artifact, store_running=False)


# --- Assessment (one paid model request) ---


def _snapshot(responses: RecordedResponses) -> list[tuple[str, Any, Any]]:
    return [(r.question_id, r.status, r.answer) for r in responses.recorded]


def _failure_message(error: ClarificationError, path: Path | None) -> str:
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
        reason = "The model declined to produce an assessment."
    elif diagnostics is not None and diagnostics.validation_errors:
        reason = "The model's response didn't match the expected assessment format."
    else:
        reason = "The model's response couldn't be used."
    saved = f" The failed attempt is recorded in {path.name}." if path is not None else ""
    return (
        f"{reason} No new assessment was produced, and nothing was retried. Your answers are "
        f"still saved.{saved}"
    )


def _assessment_view(
    batch_id: str, artifact: ClarificationArtifact, result: Reassessment, changed: bool
) -> AssessmentView:
    assert result.path is not None and result.report is not None
    report = result.report
    earlier = {q.id for q in artifact.prior_questions}
    used = [*artifact.prior_responses, *result.responses.recorded]
    open_items = report.remaining_considerations or report.unresolved_uncertainties
    return AssessmentView(
        batch_id=batch_id,
        reassessment_id=result.path.stem.removeprefix("reassessment-"),
        round=artifact.round,
        conclusion_status="current_assessment" if artifact.round == 1 else "review_conclusion",
        report=report,
        closing_note=None if open_items else NO_OPEN_CONCERNS_NOTE,
        assessed_responses=[
            AssessedResponse(
                question_id=r.question_id,
                round=1 if r.question_id in earlier else artifact.round,
                status=r.status,
                answer=r.answer,
            )
            for r in used
            if r.status is not None
        ],
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
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "An assessment of this batch is already running (perhaps from another tab). "
                "No new request was made.",
            )
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
        store.running.add(key)

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
