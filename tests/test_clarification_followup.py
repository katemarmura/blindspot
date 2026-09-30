"""The manually triggered follow-up round (round 2) of clarification.

These check behaviour and data integrity with a fake client: lineage and
reference validation, IDs across rounds, accumulated answers, saved-answer
provenance, no-call cases, failures, and that earlier files are untouched.
They do not judge question quality.
"""

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import pydantic
import pytest
from anthropic.types import Message, StopReason, TextBlock, Usage

from blindspot import clarification
from blindspot.prompts import FOLLOWUP_QUESTIONS_SYSTEM_PROMPT, REASSESSMENT_SYSTEM_PROMPT

_PLAN = "Fictional plan: open a small bakery."
_MODEL = "claude-haiku-4-5"


def _finding(label: str) -> dict[str, Any]:
    return {
        "title": f"{label} title",
        "observation": f"{label} observation.",
        "why_it_matters": "W",
        "assumptions": [],
        "question_to_check": "Q?",
    }


def _assignment(perspective: str) -> dict[str, Any]:
    return {
        "perspective": perspective,
        "objective": f"{perspective} objective.",
        "questions": ["Q?"],
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
                "assignment": _assignment("Financial"),
            },
            {
                "status": "success",
                "report": {"findings": [_finding("C")]},
                "assignment": _assignment("Operations & Logistics"),
            },
        ],
    }
    (run_dir / "result.json").write_text(json.dumps(result))
    return run_dir


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


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


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeMessages:
    messages = _FakeMessages()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))
    return messages


def _q(sources: list[str], label: str) -> dict[str, Any]:
    return {
        "question": f"{label}?",
        "why_it_matters": f"{label} matters.",
        "source_finding_ids": sources,
    }


_ROUND_1_BATCH = {
    "questions": [_q(["e1-f1"], "Runway"), _q(["e1-f2"], "Equipment"), _q(["e2-f1"], "Hours")]
}
_ROUND_1_ANSWERS = {
    "q1": {"status": "answered", "answer": "Savings cover about six months of fixed costs."},
    "q2": {"status": "dont_know"},
    "q3": {"status": "deferred"},
}
_ROUND_1_REPORT = {
    "remaining_considerations": [
        {
            "title": "Equipment",
            "observation": "O",
            "why_it_matters": "W",
            "source_finding_ids": ["e1-f2"],
            "question_ids": ["q2"],
        }
    ],
    "addressed_concerns": [
        {"summary": "Runway", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]}
    ],
    "unresolved_uncertainties": [
        {"summary": "Hours", "source_finding_ids": ["e2-f1"], "question_ids": ["q3"]}
    ],
    "takeaway": {
        "blind_spot": "Equipment remains the main open decision; opening hours are unknown.",
        "why_it_mattered": "It affects the plan.",
        "status": "open",
        "status_explanation": "A decision is still needed.",
    },
}
_FOLLOWUP_BATCH = {"questions": [_q(["e1-f2"], "Fitted"), _q(["e2-f1"], "Opening days")]}


def _fill(template: Path, updates: dict[str, dict[str, Any]]) -> None:
    data = json.loads(template.read_text())
    for entry in data["responses"]:
        entry.update(updates.get(entry["question_id"], {}))
    template.write_text(json.dumps(data, ensure_ascii=False))


def _round_1(tmp_path: Path, fake: _FakeMessages) -> tuple[Path, Path, Path]:
    """Run prepare and reassess for round 1; return (artifact, responses, reassessment)."""
    run_dir = _write_run(tmp_path)
    fake.outcomes.append(_message(json.dumps(_ROUND_1_BATCH)))
    prepared = asyncio.run(clarification.prepare(run_dir))
    artifact = prepared.directory / "clarification.json"
    responses = prepared.directory / "responses.json"
    _fill(responses, _ROUND_1_ANSWERS)
    fake.outcomes.append(_message(json.dumps(_ROUND_1_REPORT)))
    result = asyncio.run(clarification.reassess(artifact, responses))
    assert result.path is not None and result.error is None
    fake.calls.clear()
    return artifact, responses, result.path


def _digest(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _followup(reassessment: Path) -> clarification.PreparedClarification:
    return asyncio.run(clarification.prepare_followup(reassessment))


def _section(content: str, heading: str, next_heading: str | None = None) -> Any:
    part = content.split(heading + "\n", 1)[1]
    if next_heading is not None:
        part = part.split("\n\n" + next_heading, 1)[0]
    return json.loads(part)


# --- Generating the follow-up batch ---


def test_followup_generates_round_2_batch_with_lineage(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact_path, _, reassessment_path = _round_1(tmp_path, fake)
    run_dir = artifact_path.parent.parent
    before = _digest(run_dir)
    fake.outcomes.append(_message(json.dumps(_FOLLOWUP_BATCH)))

    prepared = _followup(reassessment_path)

    # One call, with the follow-up prompt and the clarification allowance.
    (call,) = fake.calls
    assert call["system"] == FOLLOWUP_QUESTIONS_SYSTEM_PROMPT
    assert call["max_tokens"] == clarification.QUESTIONS_MAX_OUTPUT_TOKENS == 15000
    content = call["messages"][0]["content"]
    assert content.startswith("Plan:\n" + _PLAN)
    first_round = _section(
        content, "First-round clarification questions:", "First-round recorded responses"
    )
    assert [q["id"] for q in first_round] == ["q1", "q2", "q3"]
    assert first_round[2]["perspectives"] == ["Operations & Logistics"]
    recorded = _section(
        content,
        "First-round recorded responses (reported by the user; not independently verified):",
        "First-round questions without a response",
    )
    assert recorded == [
        {"question_id": "q1", "status": "answered", "answer": _ROUND_1_ANSWERS["q1"]["answer"]},
        {"question_id": "q2", "status": "dont_know", "answer": None},
        {"question_id": "q3", "status": "deferred", "answer": None},
    ]
    assert (
        _section(
            content,
            "Latest reassessment (a model interpretation of the findings and responses, not user "
            "input):",
        )
        == _ROUND_1_REPORT
    )

    # A new round-2 batch next to round 1, with IDs continuing after round 1.
    artifact = prepared.artifact
    assert prepared.error is None
    assert prepared.directory.parent == run_dir
    assert prepared.directory != artifact_path.parent
    assert artifact.round == 2
    assert [q.id for q in artifact.questions] == ["q4", "q5"]
    sources = artifact.context().sources
    assert clarification.question_perspectives(artifact.questions[1], sources) == [
        "Operations & Logistics"
    ]
    round_1 = clarification.load_artifact(artifact_path)
    assert artifact.previous is not None
    assert artifact.previous.clarification_id == round_1.clarification_id
    assert artifact.previous.reassessment_file == str(reassessment_path)
    assert artifact.prior_questions == round_1.questions
    assert [r.model_dump(exclude={"question"}) for r in artifact.prior_responses] == [
        {"question_id": q, "status": a["status"], "answer": a.get("answer")}
        for q, a in _ROUND_1_ANSWERS.items()
    ]
    assert artifact.prior_reassessment is not None
    assert artifact.prior_reassessment.model_dump() == _ROUND_1_REPORT

    saved = json.loads((prepared.directory / "clarification.json").read_text())
    assert saved["round"] == 2
    assert saved["question_generation"]["stage"] == "followup"
    assert saved["question_generation"]["usage"]["model_calls"] == 1  # this call only
    template = json.loads((prepared.directory / "responses.json").read_text())
    assert [r["question_id"] for r in template["responses"]] == ["q4", "q5"]

    # Earlier files are unchanged; only the new clarify-* directory was added.
    after = _digest(run_dir)
    assert {k: after[k] for k in before} == before


def test_followup_uses_saved_answers_not_a_later_edited_response_file(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    _, responses_path, reassessment_path = _round_1(tmp_path, fake)
    _fill(responses_path, {"q1": {"status": "answered", "answer": "EDITED LATER"}})
    fake.outcomes.append(_message(json.dumps(_FOLLOWUP_BATCH)))

    prepared = _followup(reassessment_path)

    assert "EDITED LATER" not in fake.calls[0]["messages"][0]["content"]
    assert prepared.artifact.prior_responses[0].answer == _ROUND_1_ANSWERS["q1"]["answer"]


def test_zero_followup_questions_is_reported_as_no_useful_followup(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, reassessment_path = _round_1(tmp_path, fake)
    fake.outcomes.append(_message(json.dumps({"questions": []})))

    assert clarification.main(["followup", "--reassessment", str(reassessment_path)]) == 0

    out = capsys.readouterr().out
    assert "No useful follow-up" in out
    assert "not evidence that the plan is risk-free" in out
    (round_2,) = [
        d
        for d in reassessment_path.parent.parent.glob("clarify-*")
        if d != reassessment_path.parent
    ]
    template = round_2 / "responses.json"
    assert json.loads(template.read_text())["responses"] == []
    # Reassessing an empty follow-up has nothing new: no call.
    assert (
        clarification.main(
            [
                "reassess",
                "--clarification",
                str(round_2 / "clarification.json"),
                "--responses",
                str(template),
            ]
        )
        == 0
    )
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "issue_type"),
    [
        pytest.param(_message('{"questions": [', "max_tokens"), None, id="truncated"),
        pytest.param(
            _message(json.dumps({"questions": [_q(["e9-f1"], "Unknown")]})),
            "unknown_source_id",
            id="unknown-source",
        ),
    ],
)
def test_followup_failure_is_saved_with_diagnostics(
    tmp_path: Path,
    fake: _FakeMessages,
    capsys: pytest.CaptureFixture[str],
    outcome: Message,
    issue_type: str | None,
) -> None:
    artifact_path, _, reassessment_path = _round_1(tmp_path, fake)
    round_1_dir = artifact_path.parent
    before = _digest(round_1_dir)
    fake.outcomes.append(outcome)

    assert clarification.main(["followup", "--reassessment", str(reassessment_path)]) == 1

    assert len(fake.calls) == 1  # no retry, no repair
    (round_2,) = [d for d in round_1_dir.parent.glob("clarify-*") if d != round_1_dir]
    saved = json.loads((round_2 / "clarification.json").read_text())
    assert saved["status"] == "failed"
    assert saved["questions"] == []
    assert not (round_2 / "responses.json").exists()
    generation = saved["question_generation"]
    assert generation["diagnostics"]["failure_stage"] == "followup_questions"
    assert generation["call_diagnostics"]["max_output_tokens"] == 15000
    assert generation["usage"]["input_tokens"] == 900
    if issue_type is not None:
        assert issue_type in {i["type"] for i in generation["diagnostics"]["validation_errors"]}
    assert "Follow-up question generation" in capsys.readouterr().err
    assert _digest(round_1_dir) == before


# --- Invalid lineage and third rounds: rejected before any call ---


def _mutated_copy(reassessment_path: Path, mutate: Any) -> Path:
    data = json.loads(reassessment_path.read_text())
    mutate(data)
    copy = reassessment_path.with_name("reassessment-99990101T000000Z-mutant.json")
    copy.write_text(json.dumps(data))
    return copy


def _drop_last_response(data: dict[str, Any]) -> None:
    data["recorded_responses"].pop()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(lambda d: d.update(status="failed", report=None), "failed", id="failed"),
        pytest.param(lambda d: d.update(clarification_id="other"), "do not match", id="batch-id"),
        pytest.param(
            lambda d: d["recorded_responses"].append(
                {"question_id": "q9", "status": "answered", "answer": "x"}
            ),
            "recorded responses do not match",
            id="unknown-response",
        ),
        pytest.param(_drop_last_response, "do not cover the batch", id="gap"),
        pytest.param(
            lambda d: d["report"]["remaining_considerations"][0].update(question_ids=["q9"]),
            "do not resolve",
            id="report-unknown-question",
        ),
        pytest.param(
            lambda d: d["report"]["addressed_concerns"][0].update(question_ids=["q2"]),
            "do not resolve",
            id="report-addressed-by-dont-know",
        ),
        pytest.param(lambda d: d.update(clarification_round=2), "only one follow-up", id="round-2"),
    ],
)
def test_invalid_source_is_rejected_before_any_call(
    tmp_path: Path, fake: _FakeMessages, mutate: Any, match: str
) -> None:
    artifact_path, _, reassessment_path = _round_1(tmp_path, fake)
    mutant = _mutated_copy(reassessment_path, mutate)
    dirs_before = sorted(artifact_path.parent.parent.glob("clarify-*"))

    with pytest.raises(clarification.ClarificationInputError, match=match):
        _followup(mutant)

    assert fake.calls == []
    assert sorted(artifact_path.parent.parent.glob("clarify-*")) == dirs_before


def test_missing_artifact_is_rejected_before_any_call(tmp_path: Path, fake: _FakeMessages) -> None:
    _, _, reassessment_path = _round_1(tmp_path, fake)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    lonely = _mutated_copy(reassessment_path, lambda d: d.update(clarification_file="missing.json"))
    moved = Path(shutil.move(lonely, elsewhere / lonely.name))

    with pytest.raises(clarification.ClarificationInputError, match="not found"):
        _followup(moved)
    assert fake.calls == []


def _round_2(tmp_path: Path, fake: _FakeMessages) -> tuple[Path, Path, Path]:
    """Round 1 then a follow-up; return (round-1 reassessment, round-2 artifact, template)."""
    _, _, reassessment_path = _round_1(tmp_path, fake)
    fake.outcomes.append(_message(json.dumps(_FOLLOWUP_BATCH)))
    prepared = _followup(reassessment_path)
    fake.calls.clear()
    return (
        reassessment_path,
        prepared.directory / "clarification.json",
        prepared.directory / "responses.json",
    )


def test_third_round_is_rejected_before_any_call(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    _, artifact, template = _round_2(tmp_path, fake)
    _fill(template, {"q4": {"status": "answered", "answer": "Yes, fully fitted."}})
    report = {**_ROUND_1_REPORT, "unresolved_uncertainties": []}
    fake.outcomes.append(_message(json.dumps(report)))
    round_2_reassessment = asyncio.run(clarification.reassess(artifact, template)).path
    assert round_2_reassessment is not None
    fake.calls.clear()

    code = clarification.main(["followup", "--reassessment", str(round_2_reassessment)])

    assert code == 1
    assert fake.calls == []
    assert "only one follow-up round is supported" in capsys.readouterr().err


# --- Reassessing the follow-up batch ---


def test_round_2_reassessment_uses_accumulated_answers(tmp_path: Path, fake: _FakeMessages) -> None:
    round_1_reassessment, artifact, template = _round_2(tmp_path, fake)
    round_1_dir = round_1_reassessment.parent
    before = _digest(round_1_dir)
    _fill(
        template,
        {
            "q4": {"status": "answered", "answer": "The storefront is fully fitted."},
            "q5": {"status": "not_applicable", "answer": "Opening days are not decided by me."},
        },
    )
    report = {
        "remaining_considerations": [],
        "addressed_concerns": [
            # One answer from each round.
            {"summary": "Runway", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]},
            {"summary": "Equipment", "source_finding_ids": ["e1-f2"], "question_ids": ["q4"]},
        ],
        "unresolved_uncertainties": [
            {"summary": "Hours", "source_finding_ids": ["e2-f1"], "question_ids": ["q3", "q5"]}
        ],
        "takeaway": {
            "blind_spot": "Opening hours remain open.",
            "why_it_mattered": "It affects the plan.",
            "status": "open",
            "status_explanation": "A decision is still needed.",
        },
    }
    fake.outcomes.append(_message(json.dumps(report)))

    result = asyncio.run(clarification.reassess(artifact, template))

    assert result.error is None and result.path is not None
    (call,) = fake.calls
    assert call["system"] == REASSESSMENT_SYSTEM_PROMPT
    content = call["messages"][0]["content"]
    questions = _section(
        content, "Clarification questions from both rounds:", "Recorded responses from both"
    )
    assert [(q["id"], q["round"]) for q in questions] == [
        ("q1", 1),
        ("q2", 1),
        ("q3", 1),
        ("q4", 2),
        ("q5", 2),
    ]
    expected = [
        {
            "question_id": "q1",
            "status": "answered",
            "answer": _ROUND_1_ANSWERS["q1"]["answer"],
            "round": 1,
        },
        {"question_id": "q2", "status": "dont_know", "answer": None, "round": 1},
        {"question_id": "q3", "status": "deferred", "answer": None, "round": 1},
        {
            "question_id": "q4",
            "status": "answered",
            "answer": "The storefront is fully fitted.",
            "round": 2,
        },
        {
            "question_id": "q5",
            "status": "not_applicable",
            "answer": "Opening days are not decided by me.",
            "round": 2,
        },
    ]
    assert (
        _section(
            content,
            "Recorded responses from both rounds (reported by the user; not independently "
            "verified):",
            "Unanswered questions",
        )
        == expected
    )
    # The round-1 report (a model interpretation) is not part of the reassessment input.
    assert "Latest reassessment" not in content

    saved = json.loads(result.path.read_text())
    assert saved["clarification_round"] == 2
    assert saved["recorded_responses"] == expected
    assert saved["unanswered_question_ids"] == []
    assert saved["usage"]["model_calls"] == 1
    assert _digest(round_1_dir) == before


def test_round_2_addressed_concern_cannot_cite_a_prior_non_answer(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    _, artifact, template = _round_2(tmp_path, fake)
    _fill(template, {"q4": {"status": "answered", "answer": "Yes."}})
    report = {
        "remaining_considerations": [],
        "addressed_concerns": [
            {"summary": "S", "source_finding_ids": ["e1-f2"], "question_ids": ["q2"]}  # dont_know
        ],
        "unresolved_uncertainties": [],
        "takeaway": {
            "blind_spot": "T.",
            "why_it_mattered": "It affects the plan.",
            "status": "open",
            "status_explanation": "A decision is still needed.",
        },
    }
    fake.outcomes.append(_message(json.dumps(report)))

    result = asyncio.run(clarification.reassess(artifact, template))

    assert result.error is not None and result.path is not None
    saved = json.loads(result.path.read_text())
    assert {
        "schema": "reassessment_report",
        "loc": "addressed_concerns.0.question_ids.0",
        "type": "question_not_answered",
    } in saved["diagnostics"]["validation_errors"]


def test_round_2_with_only_deferred_answers_makes_no_call(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    _, artifact, template = _round_2(tmp_path, fake)
    _fill(template, {"q4": {"status": "deferred"}})

    result = asyncio.run(clarification.reassess(artifact, template))

    assert result.path is None
    assert fake.calls == []


def test_round_2_response_file_cannot_overwrite_round_1_answers(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    _, artifact, template = _round_2(tmp_path, fake)
    data = json.loads(template.read_text())
    data["responses"].append({"question_id": "q1", "status": "answered", "answer": "Changed."})
    template.write_text(json.dumps(data))

    with pytest.raises(clarification.ClarificationInputError, match="unknown question ID"):
        asyncio.run(clarification.reassess(artifact, template))
    assert fake.calls == []


# --- Compatibility and the artifact contract ---


def test_round_1_artifact_format_is_unchanged(tmp_path: Path, fake: _FakeMessages) -> None:
    artifact_path, _, _ = _round_1(tmp_path, fake)

    saved = json.loads(artifact_path.read_text())

    assert set(saved) == {
        "clarification_id",
        "created_at",
        "status",
        "source_run",
        "plan",
        "experts",
        "synthesis",
        "sources",
        "questions",
        "question_generation",
    }
    assert clarification.load_artifact(artifact_path).round == 1


def _artifact_data(artifact_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(artifact_path.read_text())
    return data


def test_artifact_contract_rejects_inconsistent_rounds(tmp_path: Path, fake: _FakeMessages) -> None:
    _, artifact_path, _ = _round_2(tmp_path, fake)
    good = _artifact_data(artifact_path)
    clarification.ClarificationArtifact.model_validate(good)

    mutations: list[Callable[[dict[str, Any]], None]] = [
        lambda d: d.update(previous=None),
        lambda d: d["questions"][0].update(id="q1"),  # clashes with a round-1 question
        lambda d: d.update(prior_unanswered_question_ids=["q1"]),  # q1 has a response
        lambda d: d.update(round=3),
    ]
    for mutate in mutations:
        data = json.loads(json.dumps(good))
        mutate(data)
        with pytest.raises(pydantic.ValidationError):
            clarification.ClarificationArtifact.model_validate(data)

    round_1_with_prior = {**good, "round": 1}
    with pytest.raises(pydantic.ValidationError):
        clarification.ClarificationArtifact.model_validate(round_1_with_prior)


# --- Development helpers understand round-2 artifacts ---


def _load_helper(skill: str, filename: str) -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / ".claude" / "skills" / skill / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_helpers_summarize_and_classify_a_round_2_run(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    round_1_reassessment, artifact, template = _round_2(tmp_path, fake)
    _fill(template, {"q4": {"status": "answered", "answer": "Fully fitted."}})
    report = {
        "remaining_considerations": [],
        "addressed_concerns": [
            {"summary": "Runway", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]}
        ],
        "unresolved_uncertainties": [],
        "takeaway": {
            "blind_spot": "The supplied answers address the runway concern raised in this review.",
            "why_it_mattered": "It affects the plan.",
            "status": "open",
            "status_explanation": "A decision is still needed.",
        },
    }
    fake.outcomes.append(_message(json.dumps(report)))
    round_2_reassessment = asyncio.run(clarification.reassess(artifact, template)).path
    assert round_2_reassessment is not None

    inspect = _load_helper("inspect-run", "summarize_run.py")
    assert inspect.main([str(artifact.parent.parent)]) == 0
    out = capsys.readouterr().out
    assert "FOLLOW-UP ROUND 2: follows" in out
    assert "round 2: used responses from every round" in out
    assert "UNRESOLVED" not in out  # q1 (round 1) cited in the round-2 report resolves
    assert "has changed since" not in out
    assert "ADDRESSED BY NON-ANSWER" not in out

    compare = _load_helper("compare-reassessments", "compare_reassessments.py")
    a = compare.load_side(round_1_reassessment)
    b = compare.load_side(round_2_reassessment)
    assert compare.classify(a, b).startswith("round change (round 1 -> round 2)")
    assert compare._structure_notes(b) == []


# --- The committed manual evaluation case ---

_FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "followup" / "kitten-after-first-round"
_FIXTURE_REASSESSMENT = "reassessment-20260101T000000Z-fixture.json"


def test_followup_fixture_is_a_valid_source() -> None:
    source = clarification.load_followup_source(_FIXTURE / _FIXTURE_REASSESSMENT)

    assert source.artifact.round == 1
    assert [(r.question_id, r.status) for r in source.responses.recorded] == [
        ("q1", "answered"),
        ("q2", "dont_know"),
        ("q3", "deferred"),
    ]
    reused = Path(__file__).resolve().parents[1] / "evals" / "reassessment"
    assert (_FIXTURE / "clarification.json").read_bytes() == (
        reused / "kitten-before-holiday" / "clarification.json"
    ).read_bytes()


def test_followup_fixture_runs_on_a_copy(tmp_path: Path, fake: _FakeMessages) -> None:
    before = _digest(_FIXTURE)
    copy = tmp_path / "runs" / "evals" / "followup-kitten" / "clarify-round-1"
    shutil.copytree(_FIXTURE, copy)
    batch = {"questions": [_q(["e3-f1"], "How old on departure day")]}
    fake.outcomes.append(_message(json.dumps(batch)))

    prepared = _followup(copy / _FIXTURE_REASSESSMENT)

    assert prepared.error is None
    assert prepared.directory.parent == copy.parent
    assert [q.id for q in prepared.artifact.questions] == ["q4"]
    assert _digest(_FIXTURE) == before


# --- Regressions: validation gaps ---


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda d: d.update(prior_responses=[], prior_unanswered_question_ids=[]),
            id="answers-silently-dropped",
        ),
        pytest.param(
            lambda d: d.update(prior_responses=d["prior_responses"][:2]),
            id="one-prior-question-unaccounted",
        ),
        pytest.param(
            lambda d: d.update(
                prior_responses=d["prior_responses"][:2],
                prior_unanswered_question_ids=["q3", "q3"],
            ),
            id="prior-question-listed-twice",
        ),
    ],
)
def test_round_2_artifact_must_account_for_every_prior_question(
    tmp_path: Path,
    fake: _FakeMessages,
    capsys: pytest.CaptureFixture[str],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    # Regression: prior questions with neither a recorded response nor an
    # unanswered entry were accepted, so earlier answers could silently vanish.
    _, artifact, template = _round_2(tmp_path, fake)
    data = _artifact_data(artifact)
    mutate(data)
    artifact.write_text(json.dumps(data))
    _fill(template, {"q4": {"status": "answered", "answer": "Yes."}})

    code = clarification.main(
        ["reassess", "--clarification", str(artifact), "--responses", str(template)]
    )

    assert code == 1
    assert fake.calls == []
    err = capsys.readouterr().err
    assert "Clarification artifact is invalid" in err
    assert "each prior question must appear exactly once" in err


@pytest.mark.parametrize("answer", [None, "   "])
def test_invalid_saved_answer_is_a_normal_input_error(
    tmp_path: Path,
    fake: _FakeMessages,
    capsys: pytest.CaptureFixture[str],
    answer: str | None,
) -> None:
    # Regression: an "answered" entry without text raised an uncaught
    # ValidationError (a traceback) instead of the normal CLI error.
    _, _, reassessment_path = _round_1(tmp_path, fake)

    def drop_answer_text(d: dict[str, Any]) -> None:
        d["recorded_responses"][0]["answer"] = answer

    mutant = _mutated_copy(reassessment_path, drop_answer_text)

    with pytest.raises(
        clarification.ClarificationInputError, match="recorded responses are invalid"
    ):
        clarification.load_followup_source(mutant)
    assert clarification.main(["followup", "--reassessment", str(mutant)]) == 1

    assert fake.calls == []
    err = capsys.readouterr().err
    assert "Error: Reassessment's recorded responses are invalid" in err
    assert "requires nonblank answer text" in err
    assert "Traceback" not in err


# --- Takeaway and the conclusion lifecycle ---

from blindspot.schemas import ReassessmentReport, SavedReassessmentReport  # noqa: E402


def test_new_reports_require_a_nonblank_takeaway_but_saved_ones_may_lack_it() -> None:
    sections = {k: v for k, v in _ROUND_1_REPORT.items() if k != "takeaway"}
    with pytest.raises(pydantic.ValidationError):
        ReassessmentReport.model_validate(sections)
    with pytest.raises(pydantic.ValidationError):
        ReassessmentReport.model_validate({**sections, "takeaway": "  "})
    insight = ReassessmentReport.model_validate(_ROUND_1_REPORT).takeaway
    assert insight is not None and insight.blind_spot.startswith("Equipment")
    legacy = SavedReassessmentReport.model_validate(sections)  # saved before takeaways
    assert legacy.takeaway is None
    with pytest.raises(pydantic.ValidationError):
        SavedReassessmentReport.model_validate({**sections, "takeaway": ""})


@pytest.mark.parametrize(
    ("takeaway", "issue_type"),
    [pytest.param(None, "missing", id="missing"), pytest.param("   ", "model_type", id="blank")],
)
def test_model_report_without_takeaway_is_a_failure_not_a_conclusion(
    tmp_path: Path,
    fake: _FakeMessages,
    capsys: pytest.CaptureFixture[str],
    takeaway: str | None,
    issue_type: str,
) -> None:
    run_dir = _write_run(tmp_path)
    fake.outcomes.append(_message(json.dumps(_ROUND_1_BATCH)))
    prepared = asyncio.run(clarification.prepare(run_dir))
    _fill(prepared.directory / "responses.json", _ROUND_1_ANSWERS)
    report = {k: v for k, v in _ROUND_1_REPORT.items() if k != "takeaway"}
    if takeaway is not None:
        report["takeaway"] = takeaway
    fake.outcomes.append(_message(json.dumps(report)))
    capsys.readouterr()

    code = clarification.main(
        [
            "reassess",
            "--clarification",
            str(prepared.directory / "clarification.json"),
            "--responses",
            str(prepared.directory / "responses.json"),
        ]
    )

    assert code == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "No new assessment was produced." in out.err
    assert "Current assessment" not in out.err
    (saved_path,) = prepared.directory.glob("reassessment-*.json")
    saved = json.loads(saved_path.read_text())
    assert saved["status"] == "failed"
    assert saved["conclusion_status"] is None
    assert saved["report"] is None
    assert {"schema": "reassessment_report", "loc": "takeaway", "type": issue_type} in saved[
        "diagnostics"
    ]["validation_errors"]


def test_round_1_takeaway_is_saved_as_the_current_assessment(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run(tmp_path)
    fake.outcomes.append(_message(json.dumps(_ROUND_1_BATCH)))
    prepared = asyncio.run(clarification.prepare(run_dir))
    _fill(prepared.directory / "responses.json", _ROUND_1_ANSWERS)
    fake.outcomes.append(_message(json.dumps(_ROUND_1_REPORT)))
    capsys.readouterr()

    code = clarification.main(
        [
            "reassess",
            "--clarification",
            str(prepared.directory / "clarification.json"),
            "--responses",
            str(prepared.directory / "responses.json"),
        ]
    )

    assert code == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == _ROUND_1_REPORT  # stdout stays machine-readable JSON
    assert "Current assessment (round 1): Blind spot: Equipment" in out.err
    assert "One follow-up round is available" in out.err
    (saved_path,) = prepared.directory.glob("reassessment-*.json")
    saved = json.loads(saved_path.read_text())
    assert saved["conclusion_status"] == "current_assessment"
    assert saved["report"]["takeaway"] == _ROUND_1_REPORT["takeaway"]
    assert saved["usage"]["model_calls"] == 1  # this call only


def test_round_2_takeaway_is_saved_as_the_review_conclusion(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    _, artifact, template = _round_2(tmp_path, fake)
    _fill(template, {"q4": {"status": "answered", "answer": "Fully fitted."}})
    report = {
        **_ROUND_1_REPORT,
        "takeaway": {
            "blind_spot": "Opening hours are the main remaining unknown.",
            "why_it_mattered": "It affects the plan.",
            "status": "open",
            "status_explanation": "A decision is still needed.",
        },
    }
    fake.outcomes.append(_message(json.dumps(report)))

    code = clarification.main(
        ["reassess", "--clarification", str(artifact), "--responses", str(template)]
    )

    assert code == 0
    err = capsys.readouterr().err
    assert (
        "Conclusion of this review (round 2): Blind spot: Opening hours are the main remaining"
        in err
    )
    assert "One follow-up round is available" not in err
    (saved_path,) = artifact.parent.glob("reassessment-*.json")
    saved = json.loads(saved_path.read_text())
    assert saved["conclusion_status"] == "review_conclusion"
    assert saved["report"]["takeaway"] == report["takeaway"]


def _strip_takeaway(path: Path) -> None:
    data = json.loads(path.read_text())
    data["report"].pop("takeaway")
    data.pop("conclusion_status")
    path.write_text(json.dumps(data))


def test_legacy_report_without_takeaway_still_starts_a_followup(
    tmp_path: Path, fake: _FakeMessages
) -> None:
    _, _, reassessment_path = _round_1(tmp_path, fake)
    _strip_takeaway(reassessment_path)  # as saved before takeaways existed
    fake.outcomes.append(_message(json.dumps(_FOLLOWUP_BATCH)))

    prepared = _followup(reassessment_path)

    assert prepared.error is None
    assert prepared.artifact.prior_reassessment is not None
    assert prepared.artifact.prior_reassessment.takeaway is None  # not invented
    reloaded = clarification.load_artifact(prepared.directory / "clarification.json")
    assert reloaded.prior_reassessment is not None
    assert reloaded.prior_reassessment.takeaway is None


def test_zero_followup_points_to_the_existing_assessment_without_another_call(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, reassessment_path = _round_1(tmp_path, fake)
    before = reassessment_path.read_bytes()
    fake.outcomes.append(_message(json.dumps({"questions": []})))

    assert clarification.main(["followup", "--reassessment", str(reassessment_path)]) == 0

    assert len(fake.calls) == 1  # only the follow-up generation call
    out = capsys.readouterr().out
    assert "No useful follow-up" in out
    assert "The conclusion available for this session is the preceding reassessment:" in out
    assert str(reassessment_path) in out
    assert "Blind spot: Equipment" in out
    # An open concern can remain even though another question would not help.
    assert "It still lists 1 remaining consideration(s) and 1 unresolved" in out
    assert "stopping here does not resolve them" in out
    assert reassessment_path.read_bytes() == before  # not rewritten
    batches = reassessment_path.parent.parent.glob("clarify-*")
    (round_2,) = [d for d in batches if d != reassessment_path.parent]
    generation = json.loads((round_2 / "clarification.json").read_text())["question_generation"]
    assert generation["no_useful_followup"] is True
    assert generation["conclusion_reassessment_file"] == str(reassessment_path)
    assert not list(round_2.glob("reassessment-*.json"))


def test_zero_followup_with_legacy_and_empty_prior_report(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run(tmp_path)
    fake.outcomes.append(_message(json.dumps(_ROUND_1_BATCH)))
    prepared = asyncio.run(clarification.prepare(run_dir))
    _fill(prepared.directory / "responses.json", _ROUND_1_ANSWERS)
    resolved = {
        "remaining_considerations": [],
        "addressed_concerns": _ROUND_1_REPORT["addressed_concerns"],
        "unresolved_uncertainties": [],
        "takeaway": {
            "blind_spot": "T.",
            "why_it_mattered": "It affects the plan.",
            "status": "open",
            "status_explanation": "A decision is still needed.",
        },
    }
    fake.outcomes.append(_message(json.dumps(resolved)))
    reassessment = asyncio.run(
        clarification.reassess(
            prepared.directory / "clarification.json", prepared.directory / "responses.json"
        )
    ).path
    assert reassessment is not None
    _strip_takeaway(reassessment)
    fake.outcomes.append(_message(json.dumps({"questions": []})))
    capsys.readouterr()

    assert clarification.main(["followup", "--reassessment", str(reassessment)]) == 0

    out = capsys.readouterr().out
    assert "No takeaway was recorded; read the detailed sections." in out
    assert "It lists no remaining considerations or unresolved uncertainties." in out


def test_round_2_without_new_context_announces_no_new_assessment(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    round_1_reassessment, artifact, template = _round_2(tmp_path, fake)
    _fill(template, {"q4": {"status": "deferred"}})

    code = clarification.main(
        ["reassess", "--clarification", str(artifact), "--responses", str(template)]
    )

    assert code == 0
    assert fake.calls == []
    err = capsys.readouterr().err
    assert "there is no new assessment" in err
    assert f"The latest assessment is still: {round_1_reassessment}" in err
    assert "Conclusion of this review" not in err


def test_followup_failure_announces_no_conclusion(
    tmp_path: Path, fake: _FakeMessages, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, reassessment_path = _round_1(tmp_path, fake)
    fake.outcomes.append(_message('{"questions": [', "max_tokens"))

    assert clarification.main(["followup", "--reassessment", str(reassessment_path)]) == 1

    out = capsys.readouterr()
    assert "conclusion available" not in out.out + out.err
    assert "Conclusion of this review" not in out.err
