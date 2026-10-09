"""HTTP access to one saved clarification batch: view its questions and save answers.

A batch is addressed by its folder name (`clarify-…`), which the application created when
it saved the batch; clients never send filesystem paths. Lookups stay inside the
configured runs directory. Only `<runs>/<run>/<batch>/` and `<runs>/evals/<run>/<batch>/`
are searched, and a folder or file whose real path leaves the runs directory (for example
through a symlink) is refused.

Answers go to the batch's standard `responses.json`, the file the `answer` and `reassess`
commands use, through the same validation and atomic writes. In a follow-up (round 2)
batch, round-1 answers are shown but can't be changed. No endpoint calls a model or loads
credentials.
"""

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from blindspot.clarification import (
    ARTIFACT_FILENAME,
    TEMPLATE_FILENAME,
    ClarificationArtifact,
    ClarificationInputError,
    load_artifact,
    load_responses,
    question_perspectives,
)
from blindspot.guided import open_responses
from blindspot.schemas import ClarificationQuestion, ResponseEntry, ResponseStatus

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
    questions: list[QuestionView]


class ResponseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required (null is allowed, and clears the response back to unanswered).
    status: ResponseStatus | None
    answer: str | None = None


# --- Locating and reading a batch ---


@dataclass
class BatchStore:
    """The configured runs directory; one lock serializes writes to response files."""

    runs_dir: Path
    lock: threading.Lock = field(default_factory=threading.Lock)

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


def _view(batch_id: str, directory: Path, artifact: ClarificationArtifact) -> BatchView:
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
        questions=views,
    )


# --- Endpoints ---

router = APIRouter(prefix="/clarifications", tags=["clarification"])


@router.get("/{batch_id}")
def get_batch(batch_id: str, store: Store) -> BatchView:
    """The batch's questions, perspectives, and current response statuses."""
    directory = store.locate(batch_id)
    return _view(batch_id, directory, _load_artifact(directory))


@router.put("/{batch_id}/responses/{question_id}")
def put_response(
    batch_id: str, question_id: str, update: ResponseUpdate, store: Store
) -> BatchView:
    """Save or replace one question's response; every other entry is kept as it was."""
    with store.lock:
        directory = store.locate(batch_id)
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
        return _view(batch_id, directory, artifact)
