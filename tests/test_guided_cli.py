"""The guided, one-question-at-a-time answering command (`clarification answer`).

All terminal input is scripted and all model calls are faked. These tests check
control flow and data integrity (what is saved, when, and that nothing else is
touched); they say nothing about question or reassessment quality.
"""

import asyncio
import builtins
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, StopReason, TextBlock, Usage

from blindspot import clarification, guided
from blindspot.schemas import ReassessmentReport

_MODEL = "claude-haiku-4-5"
_PLAN = "Fictional plan: open a small bakery."


# --- Scripted terminal and fake model ---


class ScriptedTerminal:
    """Feeds scripted lines; an exception class in the script is raised instead."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.output: list[str] = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        assert self.script, f"unexpected prompt: {prompt!r}"
        item = self.script.pop(0)
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item
        return str(item)

    def say(self, text: str) -> None:
        self.output.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def _message(text: str | None, stop_reason: StopReason = "end_turn") -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=f"{_MODEL}-20260101",
        content=[] if text is None else [TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=900, output_tokens=300),
    )


class _FakeMessages:
    def __init__(self) -> None:
        self.outcomes: list[Message] = []
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        assert self.outcomes, "unexpected model call"
        return self.outcomes.pop(0)


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeMessages:
    messages = _FakeMessages()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))
    return messages


def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)


# --- Building saved batches through the real code paths ---


def _finding(label: str) -> dict[str, Any]:
    return {
        "title": label,
        "observation": "O",
        "why_it_matters": "W",
        "assumptions": [],
        "question_to_check": "?",
    }


def _write_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / "team-20260101T000000Z-abc123"
    run_dir.mkdir(parents=True)
    result = {
        "run_id": "team-20260101T000000Z-abc123",
        "plan": _PLAN,
        "experts": [
            {
                "status": "success",
                "report": {"findings": [_finding("A"), _finding("B")]},
                "assignment": {"perspective": "Financial", "objective": "O.", "questions": ["Q?"]},
            },
            {
                "status": "success",
                "report": {"findings": [_finding("C")]},
                "assignment": {
                    "perspective": "Operations & Logistics",
                    "objective": "O.",
                    "questions": ["Q?"],
                },
            },
        ],
    }
    (run_dir / "result.json").write_text(json.dumps(result))
    return run_dir


def _q(sources: list[str], label: str) -> dict[str, Any]:
    return {
        "question": f"{label}?",
        "why_it_matters": f"{label} matters.",
        "source_finding_ids": sources,
    }


_BATCH = {
    "questions": [_q(["e1-f1"], "Runway"), _q(["e1-f2"], "Equipment"), _q(["e2-f1"], "Hours")]
}
_INSIGHT = {
    "blind_spot": "Opening hours are not yet known.",
    "why_it_mattered": "Staffing depends on them.",
    "status": "open",
    "status_explanation": "The user has not decided the hours.",
}
_REPORT: dict[str, Any] = {
    "remaining_considerations": [
        {
            "title": "Hours",
            "observation": "O",
            "why_it_matters": "W",
            "source_finding_ids": ["e2-f1"],
            "question_ids": [],
        }
    ],
    "addressed_concerns": [
        {"summary": "Runway", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]}
    ],
    "unresolved_uncertainties": [],
    "takeaway": _INSIGHT,
}


def _round_1(tmp_path: Path, fake: _FakeMessages, batch: dict[str, Any] = _BATCH) -> Path:
    """Prepare a round-1 batch; return its clarification.json (responses.json is the template)."""
    fake.outcomes.append(_message(json.dumps(batch)))
    prepared = asyncio.run(clarification.prepare(_write_run(tmp_path)))
    fake.calls.clear()
    return prepared.directory / "clarification.json"


def _guided(
    artifact: Path, script: list[Any], responses: Path | None = None, *, revisit: bool = False
) -> tuple[int, ScriptedTerminal]:
    io = ScriptedTerminal(script)
    code = asyncio.run(guided.run_guided(artifact, responses, io, revisit_deferred=revisit))
    assert io.script == [], f"unused script lines: {io.script}"
    return code, io


def _entries(path: Path) -> dict[str, tuple[Any, Any]]:
    data = json.loads(path.read_text())
    return {e["question_id"]: (e.get("status"), e.get("answer")) for e in data["responses"]}


def _digest(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


# --- Answering ---


def test_answers_questions_in_order_with_perspectives(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact = _round_1(tmp_path, fake)
    responses = artifact.parent / "responses.json"

    code, io = _guided(artifact, ["a", "Six months of savings.", "d", "n", "", "x"])

    assert code == 0
    assert "Question 1 of 3 (q1) [Financial]" in io.text
    assert "  Runway?" in io.text
    assert "  Why it matters: Runway matters." in io.text
    assert "Question 3 of 3 (q3) [Operations & Logistics]" in io.text
    assert _entries(responses) == {
        "q1": ("answered", "Six months of savings."),
        "q2": ("dont_know", None),
        "q3": ("not_applicable", None),
    }
    assert "Completed: q1 (answered), q2 (dont_know), q3 (not_applicable)" in io.text
    assert "at most one billable model request" in io.text
    assert fake.calls == []  # "x" declined reassessment


def test_invalid_choices_and_blank_answers_are_reprompted(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)

    code, io = _guided(artifact, ["z", "a", "  ", "Fitted.", "n", "No lease here.", "l", "x"])

    assert code == 0
    assert "Please choose a, d, n, l, or q." in io.text
    assert "An answer needs some text." in io.text
    assert _entries(artifact.parent / "responses.json") == {
        "q1": ("answered", "Fitted."),
        "q2": ("not_applicable", "No lease here."),
        "q3": ("deferred", None),
    }


def test_each_response_is_saved_immediately_and_other_entries_are_preserved(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)
    responses = artifact.parent / "responses.json"
    data = json.loads(responses.read_text())
    data["responses"][2].update(status="answered", answer="Mornings only.")  # answered earlier
    responses.write_text(json.dumps(data))
    before = json.loads(responses.read_text())

    # Answer q1, then Ctrl+C at q2's menu.
    code, io = _guided(artifact, ["a", "Six months.", KeyboardInterrupt])

    assert code == 130
    after = json.loads(responses.read_text())
    assert after["clarification_id"] == before["clarification_id"]
    assert after["instructions"] == before["instructions"]
    assert after["responses"][0]["question"] == before["responses"][0]["question"]
    assert _entries(responses) == {
        "q1": ("answered", "Six months."),
        "q2": (None, None),
        "q3": ("answered", "Mornings only."),
    }
    assert "the current question was not recorded" in io.text
    assert "Question 2 of 2 (q2)" in io.text  # q3 was already answered, so 2 to go


def test_interruption_mid_answer_records_nothing_for_that_question(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)

    code, io = _guided(artifact, ["a", EOFError])

    assert code == 0
    assert all(
        status is None for status, _ in _entries(artifact.parent / "responses.json").values()
    )
    assert "0 response(s) saved this session" in io.text


def test_quit_then_resume_skips_completed_and_deferred(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact = _round_1(tmp_path, fake)
    responses = artifact.parent / "responses.json"

    code, _ = _guided(artifact, ["d", "l", "q"])
    assert code == 0
    assert _entries(responses)["q3"] == (None, None)

    # Resume: q1 (dont_know) and q2 (deferred) are skipped; only q3 is asked.
    code, io = _guided(artifact, ["a", "Weekdays.", "x"])

    assert code == 0
    assert "Skipping 1 question(s) saved for later (q2); use --revisit-deferred" in io.text
    assert "Question 1 of 1 (q3)" in io.text
    assert "Saved for later: q2" in io.text


def test_revisit_deferred_asks_them_once_without_cycling(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)
    responses = artifact.parent / "responses.json"
    _guided(artifact, ["l", "l", "a", "Weekdays.", "x"])

    # Revisit: q1 answered now, q2 deferred again; q2 is not asked a second time.
    code, io = _guided(artifact, ["a", "Six months.", "l", "x"], revisit=True)

    assert code == 0
    assert io.text.count("(q2)") == 1
    assert _entries(responses)["q1"] == ("answered", "Six months.")
    assert _entries(responses)["q2"] == ("deferred", None)


# --- Files ---


def _corrupt(responses: Path, mutate: Any) -> None:
    data = json.loads(responses.read_text())
    mutate(data)
    responses.write_text(json.dumps(data))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(
            lambda d: d.update(clarification_id="other"), "different clarification", id="batch"
        ),
        pytest.param(
            lambda d: d["responses"].append({"question_id": "q1", "status": "dont_know"}),
            "duplicate response",
            id="duplicate",
        ),
        pytest.param(
            lambda d: d["responses"].append({"question_id": "q9", "status": "dont_know"}),
            "unknown question ID",
            id="unknown",
        ),
        pytest.param(
            lambda d: d["responses"][0].update(status="answered", answer=None),
            "requires nonblank answer",
            id="answered-without-text",
        ),
    ],
)
def test_invalid_response_file_is_rejected_before_any_write(
    tmp_path: Path, fake: _FakeMessages, mutate: Any, match: str
) -> None:
    artifact = _round_1(tmp_path, fake)
    responses = artifact.parent / "responses.json"
    _corrupt(responses, mutate)
    before = _digest(artifact.parent)

    code, io = _guided(artifact, [])

    assert code == 1
    assert match in io.text
    assert "Nothing was changed." in io.text
    assert _digest(artifact.parent) == before
    assert fake.calls == []


def test_malformed_or_failed_inputs_are_rejected(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact = _round_1(tmp_path, fake)
    responses = artifact.parent / "responses.json"
    responses.write_text("{not json")

    code, io = _guided(artifact, [])
    assert code == 1
    assert "could not be read as JSON" in io.text
    assert responses.read_text() == "{not json"  # never silently replaced

    failed = tmp_path / "failed-clarification.json"
    data = json.loads(artifact.read_text())
    data["status"] = "failed"
    failed.write_text(json.dumps(data))
    code, io = _guided(failed, [], tmp_path / "unused.json")
    assert code == 1
    assert not (tmp_path / "unused.json").exists()


def test_missing_response_file_is_created_from_the_template(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)
    default = artifact.parent / "responses.json"
    default_before = default.read_bytes()
    separate = tmp_path / "answers" / "guided-demo.json"
    separate.parent.mkdir()

    code, io = _guided(artifact, ["q"], separate)

    assert code == 0
    assert "Responses created from the template" in io.text
    template = json.loads(separate.read_text())
    assert template["clarification_id"] == json.loads(artifact.read_text())["clarification_id"]
    assert [e["question_id"] for e in template["responses"]] == ["q1", "q2", "q3"]
    assert default.read_bytes() == default_before  # the default file is untouched
    clarification.load_responses(separate, clarification.load_artifact(artifact))


# --- Zero questions and nothing to reassess ---


def test_zero_question_batch_needs_no_input_or_call(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact = _round_1(tmp_path, fake, {"questions": []})

    code, io = _guided(artifact, [])

    assert code == 0
    assert "This batch has no questions" in io.text
    assert fake.calls == []


def test_all_deferred_offers_no_reassessment(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact = _round_1(tmp_path, fake)

    code, io = _guided(artifact, ["l", "l", "l"])

    assert code == 0
    assert "nothing to reassess. No call was made." in io.text
    assert "[r] reassess now" not in "".join(io.prompts)
    assert fake.calls == []


# --- Reassessment choice ---


def test_explicit_reassessment_reuses_reassess_and_renders_the_conclusion(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)
    fake.outcomes.append(_message(json.dumps(_REPORT)))

    code, io = _guided(artifact, ["a", "Six months.", "d", "l", "r"])

    assert code == 0
    assert len(fake.calls) == 1
    (saved,) = artifact.parent.glob("reassessment-*.json")
    data = json.loads(saved.read_text())
    assert data["status"] == "success"
    assert data["recorded_responses"][0]["answer"] == "Six months."
    assert data["usage"]["model_calls"] == 1
    assert "Reassessment: 1 call, input tokens 900, output tokens 300" in io.text
    assert "Current assessment (round 1):" in io.text
    assert clarification.render_conclusion(ReassessmentReport.model_validate(_REPORT)) in io.text
    assert "followup --reassessment" in io.text  # suggested, never run automatically


def test_end_of_input_at_the_reassessment_choice_exits_with_answers_saved(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)

    code, io = _guided(artifact, ["a", "Six months.", "d", "d", EOFError])

    assert code == 0
    assert fake.calls == []
    assert "Answers saved in" in io.text


def test_reassessment_failure_keeps_answers_and_points_to_the_failure(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = _round_1(tmp_path, fake)
    fake.outcomes.append(_message('{"remaining_considerations": [', "max_tokens"))

    code, io = _guided(artifact, ["a", "Six months.", "d", "d", "r"])

    assert code == 1
    (saved,) = artifact.parent.glob("reassessment-*.json")
    assert json.loads(saved.read_text())["status"] == "failed"
    assert f"Reassessment failed; details saved in {saved}" in io.text
    assert "No new assessment was produced" in io.text
    assert _entries(artifact.parent / "responses.json")["q1"] == ("answered", "Six months.")
    assert "truncated" in capsys.readouterr().err


def test_answering_works_without_credentials_and_reassess_needs_them(
    tmp_path: Path, fake: _FakeMessages, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _round_1(tmp_path, fake)
    _no_credentials(monkeypatch)

    code, io = _guided(artifact, ["a", "Six months.", "d", "d", "r"])

    assert code == 1
    assert "ANTHROPIC_API_KEY" in io.text
    assert "No call was made. Your answers are saved" in io.text
    assert fake.calls == []
    assert _entries(artifact.parent / "responses.json")["q1"] == ("answered", "Six months.")


# --- Round 2 ---


def test_round_2_batch_is_answered_and_reassessed_with_accumulated_answers(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)
    _guided(artifact, ["a", "Six months.", "d", "l", "x"])
    fake.outcomes.append(_message(json.dumps(_REPORT)))
    round_1 = asyncio.run(clarification.reassess(artifact, artifact.parent / "responses.json")).path
    assert round_1 is not None
    fake.outcomes.append(_message(json.dumps({"questions": [_q(["e2-f1"], "Opening days")]})))
    round_2 = asyncio.run(clarification.prepare_followup(round_1))
    round_2_artifact = round_2.directory / "clarification.json"
    round_1_files = _digest(artifact.parent)
    report = {**_REPORT, "takeaway": {**_INSIGHT, "status": "partly_addressed"}}
    fake.outcomes.append(_message(json.dumps(report)))
    fake.calls.clear()

    code, io = _guided(round_2_artifact, ["a", "Tuesday to Saturday.", "r"])

    assert code == 0
    assert "(follow-up round 2)" in io.text
    assert "Question 1 of 1 (q4) [Operations & Logistics]" in io.text
    assert "Conclusion of this review:" in io.text
    assert "followup --reassessment" not in io.text  # no third round is suggested
    (saved,) = round_2.directory.glob("reassessment-*.json")
    recorded = json.loads(saved.read_text())["recorded_responses"]
    assert [(r["question_id"], r["round"]) for r in recorded] == [
        ("q1", 1),
        ("q2", 1),
        ("q3", 1),
        ("q4", 2),
    ]
    assert _digest(artifact.parent) == round_1_files  # earlier files untouched


def test_zero_question_followup_points_to_the_available_conclusion(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    artifact = _round_1(tmp_path, fake)
    _guided(artifact, ["a", "Six months.", "d", "d", "x"])
    fake.outcomes.append(_message(json.dumps(_REPORT)))
    round_1 = asyncio.run(clarification.reassess(artifact, artifact.parent / "responses.json")).path
    assert round_1 is not None
    fake.outcomes.append(_message(json.dumps({"questions": []})))
    round_2 = asyncio.run(clarification.prepare_followup(round_1))
    fake.calls.clear()

    code, io = _guided(round_2.directory / "clarification.json", [])

    assert code == 0
    assert "No useful follow-up was found" in io.text
    assert "The conclusion available for this session is the preceding reassessment:" in io.text
    assert "stopping here does not resolve them" in io.text
    assert fake.calls == []


# --- CLI wiring and compatibility ---


def test_cli_answer_command_uses_the_terminal(
    tmp_path: Path,
    fake: _FakeMessages,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact = _round_1(tmp_path, fake)
    lines = iter(["d", "d", "d", "x"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))

    code = clarification.main(["answer", "--clarification", str(artifact)])

    assert code == 0
    assert "Question 1 of 3 (q1) [Financial]" in capsys.readouterr().out
    assert all(s == "dont_know" for s, _ in _entries(artifact.parent / "responses.json").values())


def test_reassess_command_still_prints_report_json(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = _round_1(tmp_path, fake)
    _guided(artifact, ["a", "Six months.", "d", "d", "x"])
    fake.outcomes.append(_message(json.dumps(_REPORT)))
    capsys.readouterr()

    code = clarification.main(
        [
            "reassess",
            "--clarification",
            str(artifact),
            "--responses",
            str(artifact.parent / "responses.json"),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["takeaway"] == _INSIGHT
