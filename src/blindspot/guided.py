"""Guided, one-question-at-a-time answering of a saved clarification batch.

Run with:
    uv run python -m blindspot.clarification answer \\
        --clarification runs/team-<id>/clarify-<id>/clarification.json \\
        [--responses runs/team-<id>/clarify-<id>/responses.json] [--revisit-deferred]

Works for round-1 and round-2 batches. Each response is validated and saved
immediately (atomically) to the response file, which is the same file the
`reassess` command reads; nothing else is modified. Answering needs no API
credentials. Reassessment happens only if the user explicitly chooses it, and
reuses `clarification.reassess` (at most one billable request). Generating a
follow-up batch stays a separate command.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from blindspot.clarification import (
    TEMPLATE_FILENAME,
    ClarificationArtifact,
    ClarificationInputError,
    _print_error,
    _template,
    available_conclusion_lines,
    load_artifact,
    load_responses,
    question_perspectives,
    reassess,
    render_conclusion,
)
from blindspot.runs import write_json
from blindspot.schemas import ResponseEntry, ResponseStatus

DONE_STATUSES = ("answered", "dont_know", "not_applicable")

MENU = "[a] answer now  [d] I don't know  [n] not applicable  [l] save for later  [q] quit"
CHOICES: dict[str, ResponseStatus | None] = {
    "a": "answered",
    "d": "dont_know",
    "n": "not_applicable",
    "l": "deferred",
    "q": None,
}


class Terminal(Protocol):
    """The whole input/output boundary: easy to replace in tests."""

    def ask(self, prompt: str) -> str:
        """Return one line of input. May raise EOFError or KeyboardInterrupt."""
        ...

    def say(self, text: str) -> None: ...


class StdTerminal:
    def ask(self, prompt: str) -> str:
        return input(prompt)

    def say(self, text: str) -> None:
        print(text)


class _Stop(Exception):
    """The user quit (q or end of input) or interrupted (Ctrl+C)."""

    def __init__(self, interrupted: bool) -> None:
        super().__init__()
        self.interrupted = interrupted


def _ask(io: Terminal, prompt: str) -> str:
    try:
        return io.ask(prompt)
    except EOFError:
        raise _Stop(interrupted=False) from None
    except KeyboardInterrupt:
        raise _Stop(interrupted=True) from None


@dataclass
class ResponseBook:
    """The response file as loaded, updated one validated entry at a time."""

    path: Path
    data: dict[str, Any]

    def entry(self, question_id: str) -> dict[str, Any] | None:
        return next(
            (e for e in self.data["responses"] if e.get("question_id") == question_id), None
        )

    def status(self, question_id: str) -> Any:
        entry = self.entry(question_id)
        return entry.get("status") if entry is not None else None

    def record(self, question_id: str, status: ResponseStatus | None, answer: str | None) -> None:
        # Validate the single new entry before writing; everything else is untouched.
        # None clears a response back to unanswered (the HTTP interface allows it).
        checked = ResponseEntry(question_id=question_id, status=status, answer=answer)
        entry = self.entry(question_id)
        if entry is None:
            entry = {"question_id": question_id}
            self.data["responses"].append(entry)
        entry["status"] = checked.status
        entry["answer"] = checked.answer  # a blank explanation is saved as null
        write_json(self.path, self.data)


def open_responses(artifact: ClarificationArtifact, path: Path) -> tuple[ResponseBook, bool]:
    """Load and validate the response file, or create the standard template if missing.

    Returns (book, created). An existing invalid file raises
    `ClarificationInputError` and is never replaced.
    """
    if not path.exists():
        template = _template(artifact, artifact.context().sources)
        write_json(path, template)
        return ResponseBook(path, template), True
    load_responses(path, artifact)  # full validation: IDs, duplicates, status/text
    data = json.loads(path.read_text(encoding="utf-8"))
    return ResponseBook(path, data), False


def _question_block(
    artifact: ClarificationArtifact, index: int, total: int, question_id: str
) -> list[str]:
    question = next(q for q in artifact.questions if q.id == question_id)
    perspectives = ", ".join(question_perspectives(question, artifact.context().sources))
    return [
        "",
        f"Question {index} of {total} ({question.id}) [{perspectives or 'unknown perspective'}]",
        f"  {question.question}",
        f"  Why it matters: {question.why_it_matters}",
    ]


def _ask_one(io: Terminal) -> tuple[ResponseStatus, str | None] | None:
    """Return (status, text) for one question, or None if the user chose to quit."""
    while True:
        choice = _ask(io, f"{MENU}\n> ").strip().lower()
        if choice not in CHOICES:
            io.say("Please choose a, d, n, l, or q.")
            continue
        status = CHOICES[choice]
        if status is None:
            return None
        if status == "answered":
            while True:
                text = _ask(io, "Your answer: ").strip()
                if text:
                    return status, text
                io.say("An answer needs some text. (Ctrl+D or Ctrl+C stops without saving it.)")
        if status == "not_applicable":
            text = _ask(io, "Optional explanation (press Enter to skip): ").strip()
            return status, text or None
        return status, None


def _summary(artifact: ClarificationArtifact, book: ResponseBook) -> list[str]:
    groups: dict[str, list[str]] = {"done": [], "deferred": [], "unanswered": []}
    for q in artifact.questions:
        status = book.status(q.id)
        key = (
            "done"
            if status in DONE_STATUSES
            else "deferred"
            if status == "deferred"
            else "unanswered"
        )
        label = f"{q.id} ({status})" if key == "done" else q.id
        groups[key].append(label)
    return [
        "",
        f"Completed: {', '.join(groups['done']) or 'none'}",
        f"Saved for later: {', '.join(groups['deferred']) or 'none'}",
        f"Unanswered: {', '.join(groups['unanswered']) or 'none'}",
    ]


async def run_guided(
    artifact_path: Path,
    responses_path: Path | None,
    io: Terminal,
    *,
    revisit_deferred: bool = False,
) -> int:
    try:
        artifact = load_artifact(artifact_path)
        path = responses_path or artifact_path.parent / TEMPLATE_FILENAME
        book, created = open_responses(artifact, path)
    except ClarificationInputError as exc:
        io.say(f"Error: {exc}")
        io.say("Nothing was changed.")
        return 1

    round_label = "follow-up round 2" if artifact.round == 2 else "round 1"
    io.say(f"Clarification batch {artifact.clarification_id} ({round_label})")
    io.say(f"Responses {'created from the template' if created else 'loaded'}: {book.path}")

    if not artifact.questions:
        io.say("This batch has no questions, so there is nothing to answer and no call to make.")
        if artifact.round == 2:
            io.say(
                "No useful follow-up was found. This is not evidence that the plan is risk-free."
            )
            for line in available_conclusion_lines(artifact):
                io.say(line)
        return 0

    statuses = {q.id: book.status(q.id) for q in artifact.questions}
    skipped_deferred = [
        qid for qid, s in statuses.items() if s == "deferred" and not revisit_deferred
    ]
    pending = [
        q.id
        for q in artifact.questions
        if statuses[q.id] is None or (revisit_deferred and statuses[q.id] == "deferred")
    ]
    already = sum(1 for s in statuses.values() if s in DONE_STATUSES)
    io.say(
        f"{len(artifact.questions)} question(s) in this batch: {already} already answered, "
        f"{len(pending)} to go."
    )
    if skipped_deferred:
        io.say(
            f"Skipping {len(skipped_deferred)} question(s) saved for later "
            f"({', '.join(skipped_deferred)}); use --revisit-deferred to answer them."
        )

    saved = 0
    try:
        for position, question_id in enumerate(pending, start=1):
            for line in _question_block(artifact, position, len(pending), question_id):
                io.say(line)
            outcome = _ask_one(io)
            if outcome is None:
                raise _Stop(interrupted=False)
            status, text = outcome
            book.record(question_id, status, text)
            saved += 1
            io.say(f"Saved {question_id}: {status.replace('_', ' ')}.")
    except _Stop as stop:
        io.say("")
        io.say(
            f"{'Interrupted' if stop.interrupted else 'Stopped'}. {saved} response(s) saved this "
            f"session in {book.path}; the current question was not recorded."
        )
        io.say("Run the same command again to continue.")
        return 130 if stop.interrupted else 0

    for line in _summary(artifact, book):
        io.say(line)
    return await _offer_reassessment(artifact_path, book, artifact, io)


async def _offer_reassessment(
    artifact_path: Path, book: ResponseBook, artifact: ClarificationArtifact, io: Terminal
) -> int:
    responses = load_responses(book.path, artifact)
    if not responses.has_new_context:
        io.say(
            "No answers that could change the assessment yet (everything is unanswered or "
            "saved for later), so there is nothing to reassess. No call was made."
        )
        return 0
    io.say("")
    io.say("Reassessment makes at most one billable model request.")
    while True:
        try:
            choice = _ask(io, "[r] reassess now  [x] exit with answers saved\n> ").strip().lower()
        except _Stop:
            choice = "x"
        if choice in ("r", "x"):
            break
        io.say("Please choose r or x.")
    if choice == "x":
        io.say(f"Answers saved in {book.path}. Reassess later with the same command or `reassess`.")
        return 0

    try:
        result = await reassess(artifact_path, book.path)
    except ValueError as exc:  # invalid files or missing configuration
        io.say(f"Error: {exc}")
        io.say(f"No call was made. Your answers are saved in {book.path}.")
        return 1
    if result.path is None:
        io.say("No new context to reassess; no call was made and there is no new assessment.")
        return 0
    if result.error is not None:
        _print_error(result.error)
        io.say(f"Reassessment failed; details saved in {result.path}.")
        io.say(f"No new assessment was produced. Your answers are saved in {book.path}.")
        return 1
    assert result.report is not None
    usage = result.call.usage if result.call is not None else None
    if usage is not None:
        io.say(
            f"Reassessment: 1 call, input tokens {usage.input_tokens}, "
            f"output tokens {usage.output_tokens}"
        )
    io.say(f"Reassessment saved in {result.path}")
    label = "Current assessment (round 1)" if result.round == 1 else "Conclusion of this review"
    io.say(f"{label}:")
    io.say(render_conclusion(result.report))
    if result.round == 1:
        io.say(
            "One follow-up round is available (a separate command, one billable request): "
            f"uv run python -m blindspot.clarification followup --reassessment {result.path}"
        )
    return 0


def run_guided_cli(
    artifact_path: Path,
    responses_path: Path | None,
    revisit_deferred: bool,
    io_factory: Callable[[], Terminal] = StdTerminal,
) -> int:
    return asyncio.run(
        run_guided(artifact_path, responses_path, io_factory(), revisit_deferred=revisit_deferred)
    )
