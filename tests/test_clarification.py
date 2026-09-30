import asyncio
import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pydantic
import pytest
from anthropic.types import (
    ContentBlock,
    Message,
    RedactedThinkingBlock,
    StopReason,
    TextBlock,
    ThinkingBlock,
    Usage,
)

from blindspot import clarification
from blindspot.prompts import CLARIFICATION_QUESTIONS_SYSTEM_PROMPT, REASSESSMENT_SYSTEM_PROMPT
from blindspot.schemas import (
    ClarificationQuestion,
    ExpertDefinition,
    ExpertReport,
    Finding,
    QuestionBatch,
    ReassessmentReport,
    ResponseEntry,
    ResponseFile,
)
from blindspot.synthesis import ExpertInput, build_input

_PLAN = "Open a coffee shop in Warsaw."
_MODEL = "claude-haiku-4-5"


def _finding(label: str) -> Finding:
    return Finding(
        title=f"{label} title",
        observation=f"{label} observation.",
        why_it_matters=f"{label} matters.",
        assumptions=[f"{label} assumption"],
        question_to_check=f"{label} question?",
    )


def _assignment(perspective: str) -> ExpertDefinition:
    return ExpertDefinition(
        perspective=perspective, objective=f"{perspective} objective.", questions=["Q?"]
    )


# e1 Financial: 2 findings; e2 Technical: failed; e3 Legal: 1 finding.
_TEAM = [
    (_assignment("Financial"), ExpertReport(findings=[_finding("A"), _finding("B")])),
    (_assignment("Technical"), None),
    (_assignment("Legal & Regulatory"), ExpertReport(findings=[_finding("C")])),
]


def _result_json(
    team: list[tuple[ExpertDefinition, ExpertReport | None]] = _TEAM,
    *,
    sources: bool = True,
    synthesis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    experts = [
        {
            "perspective": a.perspective,
            "status": "success" if r is not None else "failed",
            "elapsed_seconds": 1.0,
            "model": _MODEL,
            "report": r.model_dump() if r is not None else None,
            "error": None if r is not None else "Connection error: boom",
            "diagnostics": None,
            "assignment": a.model_dump(),
            "usage": None,
        }
        for a, r in team
    ]
    data: dict[str, Any] = {
        "run_id": "team-20260101T000000Z-abc123",
        "completed_at": "2026-01-01T00:00:00+00:00",
        "plan": _PLAN,
        "status": "partial",
        "team": {"experts": [a.model_dump() for a, _ in team]},
        "team_selection": {"status": "success"},
        "experts": experts,
        "total_usage": {"model_calls": 5},
    }
    if sources:
        data["sources"] = {
            f"e{p}-f{j}": {"expert_position": p, "finding_position": j, "perspective": "x"}
            for p, (_, r) in enumerate(team, start=1)
            if r is not None
            for j in range(1, len(r.findings) + 1)
        }
    if synthesis is not None:
        data["synthesis"] = synthesis
    return data


def _write_run(tmp_path: Path, data: dict[str, Any] | None = None) -> Path:
    run_dir = tmp_path / "runs" / "team-20260101T000000Z-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(json.dumps(data if data is not None else _result_json()))
    (run_dir / "team.json").write_text("{}")
    return run_dir


def _tree_digest(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


def _message(
    text: str | None, stop_reason: StopReason = "end_turn", tokens: tuple[int, int] = (800, 200)
) -> Message:
    content: list[ContentBlock] = [] if text is None else [TextBlock(type="text", text=text)]
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=f"{_MODEL}-20260101",
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


class _FakeMessages:
    def __init__(self, outcomes: list[Message | BaseException]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        assert self._outcomes, "unexpected extra model call"
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _install(monkeypatch: pytest.MonkeyPatch, *outcomes: Message | BaseException) -> _FakeMessages:
    messages = _FakeMessages(list(outcomes))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))
    return messages


def _api_error() -> anthropic.AuthenticationError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        401,
        request=request,
        json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
    )
    return anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)


def _question(sources: list[str], label: str = "Q") -> dict[str, Any]:
    return {
        "question": f"{label}: what arrangement exists?",
        "why_it_matters": f"{label} could change the analysis.",
        "source_finding_ids": sources,
    }


_THREE_QUESTIONS = {
    "questions": [
        _question(["e1-f1", "e3-f1"], "Consolidated"),  # spans Financial and Legal
        _question(["e1-f2"], "Second"),
        _question(["e3-f1"], "Third"),
    ]
}


def _prepare(run_dir: Path) -> clarification.PreparedClarification:
    return asyncio.run(clarification.prepare(run_dir))


def _prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: dict[str, Any] = _THREE_QUESTIONS
) -> tuple[Path, Path]:
    """Prepare a batch; return (clarification.json, responses.json) paths."""
    run_dir = _write_run(tmp_path)
    _install(monkeypatch, _message(json.dumps(batch)))
    prepared = _prepare(run_dir)
    assert prepared.error is None
    return (
        prepared.directory / clarification.ARTIFACT_FILENAME,
        prepared.directory / clarification.TEMPLATE_FILENAME,
    )


def _fill(template: Path, updates: dict[str, dict[str, Any]]) -> None:
    data = json.loads(template.read_text())
    for entry in data["responses"]:
        entry.update(updates.get(entry["question_id"], {}))
    template.write_text(json.dumps(data, ensure_ascii=False))


# --- Contracts ---


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"questions": [_question(["e1-f1"]) for _ in range(4)]}, id="four-questions"),
        pytest.param({"questions": [_question([])]}, id="no-sources"),
        pytest.param({"questions": [_question(["e1-f1", "e1-f1"])]}, id="duplicate-source"),
        pytest.param({"questions": [{**_question(["e1-f1"]), "question": " "}]}, id="blank"),
        pytest.param({"questions": [{**_question(["e1-f1"]), "id": "q1"}]}, id="model-sets-id"),
        pytest.param({"questions": [], "note": "x"}, id="extra-field"),
    ],
)
def test_invalid_question_batch_is_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        QuestionBatch.model_validate(data)


def test_question_batch_allows_zero_to_three() -> None:
    assert QuestionBatch.model_validate({"questions": []}).questions == []
    assert len(QuestionBatch.model_validate(_THREE_QUESTIONS).questions) == 3


def test_clarification_question_requires_an_id() -> None:
    with pytest.raises(pydantic.ValidationError):
        ClarificationQuestion.model_validate(_question(["e1-f1"]))
    assert ClarificationQuestion.model_validate({**_question(["e1-f1"]), "id": "q1"}).id == "q1"


@pytest.mark.parametrize(
    ("entry", "expected_answer"),
    [
        pytest.param(
            {"status": "answered", "answer": "We have 6 months."},
            "We have 6 months.",
            id="answered",
        ),
        pytest.param({"status": "dont_know"}, None, id="dont-know"),
        pytest.param({"status": "not_applicable"}, None, id="na-bare"),
        pytest.param(
            {"status": "not_applicable", "answer": "No lease."}, "No lease.", id="na-explained"
        ),
        pytest.param({"status": "deferred", "answer": "  "}, None, id="deferred-blank-text"),
        pytest.param({"status": None, "answer": None}, None, id="unanswered"),
    ],
)
def test_valid_response_entries(entry: dict[str, Any], expected_answer: str | None) -> None:
    parsed = ResponseEntry.model_validate({"question_id": "q1", **entry})
    assert parsed.answer == expected_answer


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param({"status": "answered"}, id="answered-no-text"),
        pytest.param({"status": "answered", "answer": "   "}, id="answered-blank"),
        pytest.param({"status": "dont_know", "answer": "maybe"}, id="dont-know-with-text"),
        pytest.param({"status": "deferred", "answer": "later"}, id="deferred-with-text"),
        pytest.param({"status": None, "answer": "forgot status"}, id="text-without-status"),
        pytest.param({"status": "skipped"}, id="unknown-status"),
        pytest.param({"status": "answered", "answer": "x", "confidence": 1}, id="extra-field"),
    ],
)
def test_invalid_response_entries(entry: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        ResponseEntry.model_validate({"question_id": "q1", **entry})


def _remaining(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "T",
        "observation": "O",
        "why_it_matters": "W",
        "source_finding_ids": ["e1-f1"],
        "question_ids": [],
        **overrides,
    }


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"remaining_considerations": [_remaining() for _ in range(6)]}, id="six"),
        pytest.param(
            {"remaining_considerations": [_remaining(source_finding_ids=[])]}, id="no-src"
        ),
        pytest.param(
            {
                "addressed_concerns": [
                    {"summary": "S", "source_finding_ids": ["e1-f1"], "question_ids": []}
                ]
            },
            id="addressed-without-question",
        ),
        pytest.param(
            {
                "unresolved_uncertainties": [
                    {"summary": "S", "source_finding_ids": [], "question_ids": []}
                ]
            },
            id="uncertainty-without-reference",
        ),
        pytest.param(
            {"remaining_considerations": [_remaining(question_ids=["q1", "q1"])]}, id="dup-question"
        ),
    ],
)
def test_invalid_reassessment_report_is_rejected(data: dict[str, Any]) -> None:
    full = {
        "remaining_considerations": [],
        "addressed_concerns": [],
        "unresolved_uncertainties": [],
        "takeaway": {
            "blind_spot": "A valid takeaway.",
            "why_it_mattered": "It affects the plan.",
            "status": "open",
            "status_explanation": "A decision is still needed.",
        },
        **data,
    }
    with pytest.raises(pydantic.ValidationError):
        ReassessmentReport.model_validate(full)


# --- Loading a saved run ---


def test_load_run_derives_sources_for_runs_without_saved_mapping(tmp_path: Path) -> None:
    run_dir = _write_run(tmp_path, _result_json(sources=False))

    run_id, context = clarification.load_run(run_dir)

    assert run_id == "team-20260101T000000Z-abc123"
    assert list(context.sources) == ["e1-f1", "e1-f2", "e3-f1"]
    assert context.experts[1].report is None
    assert context.synthesis is None


@pytest.mark.parametrize(
    ("data", "match"),
    [
        pytest.param(
            {**_result_json(), "sources": {"e9-f1": {"expert_position": 9, "finding_position": 1}}},
            "saved source IDs",
            id="mismatched-sources",
        ),
        pytest.param(
            {
                **_result_json(),
                "synthesis": {
                    "status": "completed",
                    "report": {
                        "findings": [
                            {
                                "title": "T",
                                "observation": "O",
                                "why_it_matters": "W",
                                "assumptions": [],
                                "question_to_check": "Q?",
                                "source_finding_ids": ["e2-f1"],
                            }
                        ]
                    },
                },
            },
            "synthesis cites unknown",
            id="bad-synthesis",
        ),
        pytest.param({"plan": _PLAN}, "invalid", id="not-a-result"),
    ],
)
def test_load_run_rejects_inconsistent_results(
    tmp_path: Path, data: dict[str, Any], match: str
) -> None:
    run_dir = _write_run(tmp_path, data)

    with pytest.raises(clarification.ClarificationInputError, match=match):
        clarification.load_run(run_dir)


def test_load_run_rejects_status_report_mismatch(tmp_path: Path) -> None:
    data = _result_json()
    data["experts"][1]["status"] = "success"  # but report is None
    run_dir = _write_run(tmp_path, data)

    with pytest.raises(clarification.ClarificationInputError, match="status does not match"):
        clarification.load_run(run_dir)


# --- prepare ---


@pytest.mark.parametrize(
    "team",
    [
        pytest.param([(_assignment("Financial"), None)], id="all-failed"),
        pytest.param([(_assignment("Financial"), ExpertReport(findings=[]))], id="all-empty"),
    ],
)
def test_prepare_without_successful_findings_makes_no_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, team: Any
) -> None:
    run_dir = _write_run(tmp_path, _result_json(team))
    fake = _install(monkeypatch)
    before = _tree_digest(run_dir)

    with pytest.raises(
        clarification.ClarificationInputError, match="no successful expert findings"
    ):
        _prepare(run_dir)

    assert fake.calls == []
    assert _tree_digest(run_dir) == before  # no clarify-* directory created


def test_prepare_success_saves_artifact_and_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synthesis = {
        "status": "completed",
        "report": {
            "findings": [
                {
                    "title": "Synth",
                    "observation": "O",
                    "why_it_matters": "W",
                    "assumptions": [],
                    "question_to_check": "Q?",
                    "source_finding_ids": ["e1-f1", "e3-f1"],
                }
            ]
        },
    }
    run_dir = _write_run(tmp_path, _result_json(synthesis=synthesis))
    before = _tree_digest(run_dir)
    fake = _install(monkeypatch, _message(json.dumps(_THREE_QUESTIONS)))

    prepared = _prepare(run_dir)

    # One call with the original plan, successful findings, missing coverage, and synthesis.
    (call,) = fake.calls
    assert call["system"] == CLARIFICATION_QUESTIONS_SYSTEM_PROMPT
    assert call["max_tokens"] == 15000
    content = call["messages"][0]["content"]
    experts = [ExpertInput(p, a, r) for p, (a, r) in enumerate(_TEAM, start=1)]
    assert content.startswith(build_input(_PLAN, experts))
    assert "Existing synthesis of these findings" in content
    assert '"Synth"' in content

    # IDs assigned after validation, in order; perspectives derived from references.
    artifact = prepared.artifact
    assert artifact.status == "success"
    assert [q.id for q in artifact.questions] == ["q1", "q2", "q3"]
    sources = artifact.context().sources
    assert clarification.question_perspectives(artifact.questions[0], sources) == [
        "Financial",
        "Legal & Regulatory",
    ]
    assert clarification.question_perspectives(artifact.questions[1], sources) == ["Financial"]

    saved = json.loads((prepared.directory / "clarification.json").read_text())
    assert saved["plan"] == _PLAN
    assert saved["source_run"] == {
        "directory": str(run_dir),
        "run_id": "team-20260101T000000Z-abc123",
    }
    assert saved["experts"][1] == {
        "position": 2,
        "assignment": _TEAM[1][0].model_dump(),
        "report": None,
    }
    assert saved["synthesis"] == synthesis["report"]
    generation = saved["question_generation"]
    assert generation["status"] == "success"
    assert generation["model"] == f"{_MODEL}-20260101"
    assert generation["usage"] == {
        "model_calls": 1,
        "input_tokens": 800,
        "output_tokens": 200,
        "usage_incomplete": False,
    }
    assert generation["error"] is None

    template = json.loads((prepared.directory / "responses.json").read_text())
    assert template["clarification_id"] == artifact.clarification_id
    assert [(r["question_id"], r["status"], r["answer"]) for r in template["responses"]] == [
        ("q1", None, None),
        ("q2", None, None),
        ("q3", None, None),
    ]
    assert "[Financial, Legal & Regulatory]" in template["responses"][0]["question"]
    ResponseFile.model_validate(template)  # the template itself is a valid response file

    # The run's own files are untouched; only a new clarify-* directory was added.
    after = _tree_digest(run_dir)
    assert {k: v for k, v in after.items() if not k.startswith("clarify-")} == before
    assert prepared.directory.parent == run_dir
    assert prepared.directory.name.startswith("clarify-")


def test_prepare_cli_prints_questions_with_perspectives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run(tmp_path)
    _install(monkeypatch, _message(json.dumps(_THREE_QUESTIONS)))

    assert clarification.main(["prepare", "--run", str(run_dir)]) == 0

    out = capsys.readouterr()
    assert "q1 [Financial, Legal & Regulatory]\n  Consolidated: what arrangement exists?" in out.out
    assert "  Based on: e1-f1, e3-f1" in out.out
    assert "Question generation usage (this stage only): 1 call, input tokens 800" in out.err
    assert "responses.json" in out.err


def test_prepare_zero_questions_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run(tmp_path)
    _install(monkeypatch, _message(json.dumps({"questions": []})))

    assert clarification.main(["prepare", "--run", str(run_dir)]) == 0

    assert "No clarification questions" in capsys.readouterr().out
    (directory,) = run_dir.glob("clarify-*")
    assert json.loads((directory / "responses.json").read_text())["responses"] == []


@pytest.mark.parametrize(
    ("outcome", "stop_reason", "issue"),
    [
        pytest.param(
            _message(json.dumps({"questions": [_question(["e1-f1", "e2-f1"])]})),
            "end_turn",
            ("question_batch", "questions.0.source_finding_ids.1", "unknown_source_id"),
            id="unknown-source",  # e2 failed, so it has no findings
        ),
        pytest.param(
            _message(json.dumps({"questions": [_question(["e1-f1"]) for _ in range(4)]})),
            "end_turn",
            ("question_batch", "questions", "too_long"),
            id="four-questions",
        ),
        pytest.param(_message(None, "refusal"), "refusal", None, id="refusal"),
        pytest.param(_message('{"questions": [', "max_tokens"), "max_tokens", None, id="truncated"),
        pytest.param(_api_error(), None, None, id="api-error"),
    ],
)
def test_prepare_failure_is_saved_with_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: Message | BaseException,
    stop_reason: str | None,
    issue: tuple[str, str, str] | None,
) -> None:
    run_dir = _write_run(tmp_path)
    fake = _install(monkeypatch, outcome)

    assert clarification.main(["prepare", "--run", str(run_dir)]) == 1

    assert len(fake.calls) == 1  # no retry, no repair
    (directory,) = run_dir.glob("clarify-*")
    assert not (directory / "responses.json").exists()
    saved = json.loads((directory / "clarification.json").read_text())
    assert saved["status"] == "failed"
    assert saved["questions"] == []
    generation = saved["question_generation"]
    assert generation["diagnostics"]["failure_stage"] == "clarification_questions"
    assert generation["diagnostics"]["stop_reason"] == stop_reason
    if isinstance(outcome, BaseException):
        assert generation["model"] is None
        assert generation["usage"]["usage_incomplete"] is True
    else:
        assert generation["model"] == f"{_MODEL}-20260101"
        assert generation["usage"]["input_tokens"] == 800
    if issue is not None:
        schema, loc, kind = issue
        assert {"schema": schema, "loc": loc, "type": kind} in generation["diagnostics"][
            "validation_errors"
        ]
    assert "dummy-test-key" not in capsys.readouterr().err


# --- reassess ---


def _reassess_cli(artifact: Path, responses: Path) -> int:
    return clarification.main(
        ["reassess", "--clarification", str(artifact), "--responses", str(responses)]
    )


_REPORT: dict[str, Any] = {
    "remaining_considerations": [
        {
            "title": "Legal permits",
            "observation": "If permits are still pending, opening could slip.",
            "why_it_matters": "It affects the timeline.",
            "source_finding_ids": ["e3-f1"],
            "question_ids": ["q3"],
        }
    ],
    "addressed_concerns": [
        {
            "summary": "The user reports six months of reserves.",
            "source_finding_ids": ["e1-f1"],
            "question_ids": ["q1"],
        }
    ],
    "unresolved_uncertainties": [
        {
            "summary": "Supplier terms remain unknown.",
            "source_finding_ids": [],
            "question_ids": ["q2"],
        }
    ],
    "takeaway": {
        "blind_spot": "Permit timing is the main open question; supplier terms remain unknown.",
        "why_it_mattered": "It affects the plan.",
        "status": "open",
        "status_explanation": "A decision is still needed.",
    },
}


def test_reassess_receives_original_context_and_exact_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    answer = "We have ~6 months of reserves — «zł» included.\nSecond line."
    _fill(
        template,
        {
            "q1": {"status": "answered", "answer": answer},
            "q2": {"status": "dont_know"},
            # q3 left unanswered (status null)
        },
    )
    before = _tree_digest(artifact_path.parent)
    fake = _install(monkeypatch, _message(json.dumps(_REPORT), tokens=(1200, 300)))

    assert _reassess_cli(artifact_path, template) == 0

    (call,) = fake.calls
    assert call["system"] == REASSESSMENT_SYSTEM_PROMPT
    assert call["max_tokens"] == 15000
    content = call["messages"][0]["content"]
    experts = [ExpertInput(p, a, r) for p, (a, r) in enumerate(_TEAM, start=1)]
    assert content.startswith(build_input(_PLAN, experts))  # original plan and findings
    recorded = json.loads(
        content.split("not independently verified):\n")[1].split("\n\nUnanswered")[0]
    )
    assert recorded == [
        {"question_id": "q1", "status": "answered", "answer": answer},
        {"question_id": "q2", "status": "dont_know", "answer": None},
    ]
    assert json.loads(content.split("(no response recorded):\n")[1]) == ["q3"]
    questions = json.loads(content.split("Clarification questions:\n")[1].split("\n\nRecorded")[0])
    assert [q["id"] for q in questions] == ["q1", "q2", "q3"]
    assert questions[0]["perspectives"] == ["Financial", "Legal & Regulatory"]

    out = capsys.readouterr()
    assert json.loads(out.out) == _REPORT
    assert "Reassessment usage (this stage only): 1 call, input tokens 1200" in out.err

    # Saved separately; the artifact and response file are unchanged.
    after = _tree_digest(artifact_path.parent)
    (new_file,) = set(after) - set(before)
    assert new_file.startswith("reassessment-")
    assert {k: after[k] for k in before} == before
    saved = json.loads((artifact_path.parent / new_file).read_text())
    assert saved["status"] == "success"
    assert saved["report"] == _REPORT
    assert saved["recorded_responses"] == recorded
    assert saved["unanswered_question_ids"] == ["q3"]
    assert saved["model"] == f"{_MODEL}-20260101"
    assert saved["usage"] == {
        "model_calls": 1,
        "input_tokens": 1200,
        "output_tokens": 300,
        "usage_incomplete": False,
    }


def test_not_applicable_and_deferred_statuses_are_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(
        template,
        {
            "q1": {"status": "not_applicable", "answer": "We are not signing a lease."},
            "q2": {"status": "deferred"},
            "q3": {"status": "not_applicable"},
        },
    )
    report = {**_REPORT, "unresolved_uncertainties": []}
    fake = _install(monkeypatch, _message(json.dumps(report)))

    assert _reassess_cli(artifact_path, template) == 0

    content = fake.calls[0]["messages"][0]["content"]
    recorded = json.loads(
        content.split("not independently verified):\n")[1].split("\n\nUnanswered")[0]
    )
    assert recorded == [
        {"question_id": "q1", "status": "not_applicable", "answer": "We are not signing a lease."},
        {"question_id": "q2", "status": "deferred", "answer": None},
        {"question_id": "q3", "status": "not_applicable", "answer": None},
    ]


@pytest.mark.parametrize(
    "updates",
    [
        pytest.param({}, id="all-unanswered"),
        pytest.param({q: {"status": "deferred"} for q in ("q1", "q2", "q3")}, id="all-deferred"),
        pytest.param({"q1": {"status": "deferred"}}, id="deferred-and-missing"),
    ],
)
def test_no_new_context_makes_no_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    updates: dict[str, dict[str, Any]],
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(template, updates)
    fake = _install(monkeypatch)
    before = _tree_digest(artifact_path.parent)

    assert _reassess_cli(artifact_path, template) == 0

    assert fake.calls == []
    assert "No new context to reassess" in capsys.readouterr().err
    assert _tree_digest(artifact_path.parent) == before


def test_omitted_responses_count_as_unanswered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    data = json.loads(template.read_text())
    data["responses"] = [{"question_id": "q2", "status": "answered", "answer": "Yes."}]
    template.write_text(json.dumps(data))
    fake = _install(monkeypatch, _message(json.dumps({**_REPORT, "addressed_concerns": []})))

    assert _reassess_cli(artifact_path, template) == 0

    content = fake.calls[0]["messages"][0]["content"]
    assert json.loads(content.split("(no response recorded):\n")[1]) == ["q1", "q3"]


def test_zero_question_batch_has_nothing_to_reassess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch, {"questions": []})
    fake = _install(monkeypatch)

    assert _reassess_cli(artifact_path, template) == 0

    assert fake.calls == []
    assert "No new context" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(
            lambda d: d.update(clarification_id="other"),
            "different clarification",
            id="wrong-batch",
        ),
        pytest.param(
            lambda d: d["responses"].append({"question_id": "q9", "status": "dont_know"}),
            "unknown question ID",
            id="unknown-question",
        ),
        pytest.param(
            lambda d: d["responses"].append({"question_id": "q1", "status": "dont_know"}),
            "duplicate response",
            id="duplicate",
        ),
        pytest.param(
            lambda d: d["responses"][0].update(status="answered", answer=""),
            "requires nonblank answer",
            id="answered-without-text",
        ),
        pytest.param(
            lambda d: d["responses"][0].update(status="dont_know", answer="hmm"),
            "not allowed",
            id="dont-know-with-text",
        ),
        pytest.param(lambda d: d.update(extra=True), "invalid", id="extra-field"),
    ],
)
def test_invalid_response_files_are_rejected_before_any_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mutate: Any,
    match: str,
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    data = json.loads(template.read_text())
    data["responses"][1].update(status="answered", answer="Valid.")
    mutate(data)
    template.write_text(json.dumps(data))
    fake = _install(monkeypatch)

    assert _reassess_cli(artifact_path, template) == 1

    assert fake.calls == []
    assert match in capsys.readouterr().err


def test_unreadable_response_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    template.write_text("{not json")
    _install(monkeypatch)

    assert _reassess_cli(artifact_path, template) == 1
    assert _reassess_cli(artifact_path, template.with_name("missing.json")) == 1

    err = capsys.readouterr().err
    assert "could not be read as JSON" in err
    assert "not found" in err


def test_failed_question_artifact_cannot_be_reassessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run(tmp_path)
    _install(monkeypatch, _message(None, "refusal"))
    prepared = _prepare(run_dir)
    responses = prepared.directory / "responses.json"
    responses.write_text(
        json.dumps({"clarification_id": prepared.artifact.clarification_id, "responses": []})
    )
    fake = _install(monkeypatch)

    assert _reassess_cli(prepared.directory / "clarification.json", responses) == 1

    assert fake.calls == []
    assert "failed question-generation call" in capsys.readouterr().err


def _bad_report(section: str, item: dict[str, Any]) -> dict[str, Any]:
    return {**_REPORT, section: [item]}


@pytest.mark.parametrize(
    ("report", "issue"),
    [
        pytest.param(
            _bad_report(
                "remaining_considerations",
                {**_REPORT["remaining_considerations"][0], "source_finding_ids": ["e2-f1"]},
            ),
            ("remaining_considerations.0.source_finding_ids.0", "unknown_source_id"),
            id="unknown-finding",
        ),
        pytest.param(
            _bad_report(
                "unresolved_uncertainties",
                {"summary": "S", "source_finding_ids": [], "question_ids": ["q7"]},
            ),
            ("unresolved_uncertainties.0.question_ids.0", "unknown_question_id"),
            id="unknown-question",
        ),
        pytest.param(
            _bad_report(
                "addressed_concerns",
                {"summary": "S", "source_finding_ids": ["e1-f2"], "question_ids": ["q2"]},
            ),
            ("addressed_concerns.0.question_ids.0", "question_not_answered"),
            id="addressed-by-dont-know",
        ),
        pytest.param(
            _bad_report(
                "addressed_concerns",
                {"summary": "S", "source_finding_ids": ["e3-f1"], "question_ids": ["q3"]},
            ),
            ("addressed_concerns.0.question_ids.0", "question_not_answered"),
            id="addressed-by-unanswered",
        ),
    ],
)
def test_reassessment_references_are_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], issue: tuple[str, str]
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(template, {"q1": {"status": "answered", "answer": "Yes."}, "q2": {"status": "dont_know"}})
    fake = _install(monkeypatch, _message(json.dumps(report)))

    assert _reassess_cli(artifact_path, template) == 1

    assert len(fake.calls) == 1
    (saved_path,) = artifact_path.parent.glob("reassessment-*.json")
    saved = json.loads(saved_path.read_text())
    assert saved["status"] == "failed"
    assert saved["report"] is None
    assert saved["model"] == f"{_MODEL}-20260101"
    assert saved["usage"]["input_tokens"] == 800
    assert saved["diagnostics"]["failure_stage"] == "reassessment"
    assert {"schema": "reassessment_report", "loc": issue[0], "type": issue[1]} in saved[
        "diagnostics"
    ]["validation_errors"]
    assert saved["recorded_responses"][0]["answer"] == "Yes."


def test_reassessment_api_error_keeps_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(template, {"q1": {"status": "answered", "answer": "Yes."}})
    _install(monkeypatch, _api_error())

    assert _reassess_cli(artifact_path, template) == 1

    (saved_path,) = artifact_path.parent.glob("reassessment-*.json")
    saved = json.loads(saved_path.read_text())
    assert saved["model"] is None
    assert saved["diagnostics"]["stop_reason"] is None
    assert saved["usage"]["usage_incomplete"] is True
    assert "dummy-test-key" not in capsys.readouterr().err + saved_path.read_text()


def test_reassessing_twice_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(template, {"q1": {"status": "answered", "answer": "Yes."}})
    _install(monkeypatch, _message(json.dumps(_REPORT)), _message(json.dumps(_REPORT)))

    assert _reassess_cli(artifact_path, template) == 0
    assert _reassess_cli(artifact_path, template) == 0

    assert len(list(artifact_path.parent.glob("reassessment-*.json"))) == 2


def test_reassessment_output_does_not_leak_unknown_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(template, {"q1": {"status": "answered", "answer": "Yes."}})
    marker = "EVIL-REASSESS-KEY"
    _install(monkeypatch, _message(json.dumps({**_REPORT, marker: marker})))

    assert _reassess_cli(artifact_path, template) == 1

    (saved_path,) = artifact_path.parent.glob("reassessment-*.json")
    assert marker not in saved_path.read_text()


def test_artifact_with_duplicate_question_ids_is_rejected_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: two different questions both named q1 were accepted, and one
    # answer was then attached to both.
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    artifact = json.loads(artifact_path.read_text())
    artifact["questions"][1]["id"] = "q1"
    artifact_path.write_text(json.dumps(artifact))
    _fill(template, {"q1": {"status": "answered", "answer": "Yes."}})
    fake = _install(monkeypatch)
    before = _tree_digest(artifact_path.parent)

    assert _reassess_cli(artifact_path, template) == 1

    assert fake.calls == []
    assert "Clarification artifact is invalid" in (err := capsys.readouterr().err)
    assert "duplicate question IDs: q1" in err
    assert _tree_digest(artifact_path.parent) == before  # nothing saved
    with pytest.raises(clarification.ClarificationInputError, match="duplicate question IDs"):
        clarification.load_artifact(artifact_path)


# --- Call diagnostics ---


def _blocks_message(blocks: list[ContentBlock], stop_reason: StopReason = "end_turn") -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=f"{_MODEL}-20260101",
        content=blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=800, output_tokens=200),
    )


def _fixed_clock(monkeypatch: pytest.MonkeyPatch, *times: float) -> None:
    ticks = iter(times)
    # Replace only the clarification module's `time` (asyncio keeps the real clock).
    monkeypatch.setattr(
        "blindspot.clarification.time", SimpleNamespace(monotonic=lambda: next(ticks))
    )


def _answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    artifact_path, template = _prepared(tmp_path, monkeypatch)
    _fill(template, {"q1": {"status": "answered", "answer": "Yes."}})
    return artifact_path, template


def _saved_reassessment(artifact_path: Path) -> dict[str, Any]:
    (path,) = artifact_path.parent.glob("reassessment-*.json")
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_successful_question_call_records_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _write_run(tmp_path)
    text = json.dumps(_THREE_QUESTIONS)
    _install(monkeypatch, _message(text))
    _fixed_clock(monkeypatch, 100.0, 101.25)

    assert clarification.main(["prepare", "--run", str(run_dir)]) == 0

    (directory,) = run_dir.glob("clarify-*")
    generation = json.loads((directory / "clarification.json").read_text())["question_generation"]
    assert generation["elapsed_seconds"] == 1.25
    assert generation["call_diagnostics"] == {
        "requested_model": _MODEL,
        "max_output_tokens": 15000,
        "elapsed_seconds": 1.25,
        "response_received": True,
        "returned_model": f"{_MODEL}-20260101",
        "stop_reason": "end_turn",
        "content_blocks": {"text": 1},
        "text_characters": len(text),
        "parse_text_is_valid_json": True,
        "usage": {
            "model_calls": 1,
            "input_tokens": 800,
            "output_tokens": 200,
            "usage_incomplete": False,
        },
    }
    # Successful CLI output stays as before: no diagnostic summary.
    err = capsys.readouterr().err
    assert "Call: requested" not in err
    assert "Response: model" not in err


def test_successful_reassessment_records_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    _install(monkeypatch, _message(json.dumps(_REPORT), tokens=(3000, 4068)))

    assert _reassess_cli(artifact_path, template) == 0

    diagnostics = _saved_reassessment(artifact_path)["call_diagnostics"]
    assert diagnostics["max_output_tokens"] == clarification.REASSESSMENT_MAX_OUTPUT_TOKENS
    assert diagnostics["stop_reason"] == "end_turn"
    assert diagnostics["parse_text_is_valid_json"] is True
    # Usage is only this stage's call, exactly as the API reported it.
    assert diagnostics["usage"] == {
        "model_calls": 1,
        "input_tokens": 3000,
        "output_tokens": 4068,
        "usage_incomplete": False,
    }


def test_truncation_with_incomplete_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    text = json.dumps(_REPORT)[:120]
    _install(monkeypatch, _message(text, "max_tokens"))
    _fixed_clock(monkeypatch, 10.0, 52.5)

    assert _reassess_cli(artifact_path, template) == 1

    saved = _saved_reassessment(artifact_path)
    assert saved["status"] == "failed"
    assert saved["elapsed_seconds"] == 42.5
    assert saved["usage"]["output_tokens"] == 200
    call = saved["call_diagnostics"]
    assert call["stop_reason"] == "max_tokens"
    assert call["text_characters"] == 120
    assert call["parse_text_is_valid_json"] is False
    assert call["elapsed_seconds"] == 42.5
    assert text not in json.dumps(saved)  # raw failed output is never saved
    err = capsys.readouterr().err
    assert "Call: requested claude-haiku-4-5, max output tokens 15000, elapsed 42.5s" in err
    assert (
        "Response: model claude-haiku-4-5-20260101, stop reason max_tokens, "
        "blocks [text x1], text 120 chars, valid JSON syntax: no"
    ) in err
    assert text not in err


def test_truncation_with_syntactically_valid_json_is_still_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    _install(monkeypatch, _message(json.dumps(_REPORT), "max_tokens"))

    assert _reassess_cli(artifact_path, template) == 1

    saved = _saved_reassessment(artifact_path)
    assert saved["status"] == "failed"
    assert saved["report"] is None
    assert saved["diagnostics"]["stop_reason"] == "max_tokens"
    assert saved["call_diagnostics"]["parse_text_is_valid_json"] is True
    assert "valid JSON syntax: yes" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("report", "issue_type"),
    [
        pytest.param(
            {**_REPORT, "remaining_considerations": "not a list"}, "list_type", id="schema"
        ),
        pytest.param(
            {
                **_REPORT,
                "addressed_concerns": [
                    {**_REPORT["addressed_concerns"][0], "question_ids": ["q9"]}
                ],
            },
            "unknown_question_id",
            id="reference",
        ),
    ],
)
def test_validation_failures_keep_both_call_and_validation_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], issue_type: str
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    _install(monkeypatch, _message(json.dumps(report)))

    assert _reassess_cli(artifact_path, template) == 1

    saved = _saved_reassessment(artifact_path)
    # Syntax is valid; schema/reference validation is reported separately.
    assert saved["call_diagnostics"]["parse_text_is_valid_json"] is True
    assert saved["call_diagnostics"]["stop_reason"] == "end_turn"
    assert issue_type in {i["type"] for i in saved["diagnostics"]["validation_errors"]}


def test_multiple_block_types_are_counted_without_persisting_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    report_text = json.dumps(_REPORT)
    extra_text = "TRAILING-TEXT-MARKER"
    blocks: list[ContentBlock] = [
        ThinkingBlock(type="thinking", thinking="THINKING-MARKER", signature="SIGNATURE-MARKER"),
        RedactedThinkingBlock(type="redacted_thinking", data="REDACTED-MARKER"),
        TextBlock(type="text", text=report_text),  # the first text block is parsed
        TextBlock(type="text", text=extra_text),
    ]
    _install(monkeypatch, _blocks_message(blocks))

    assert _reassess_cli(artifact_path, template) == 0

    saved = _saved_reassessment(artifact_path)
    call = saved["call_diagnostics"]
    assert call["content_blocks"] == {"thinking": 1, "redacted_thinking": 1, "text": 2}
    assert call["text_characters"] == len(report_text) + len(extra_text)
    assert call["parse_text_is_valid_json"] is True
    serialized = json.dumps(saved) + capsys.readouterr().err
    for marker in ("THINKING-MARKER", "SIGNATURE-MARKER", "REDACTED-MARKER", extra_text):
        assert marker not in serialized


def test_response_without_text_has_null_json_validity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    thinking = ThinkingBlock(type="thinking", thinking="THINKING-MARKER", signature="SIG")
    _install(monkeypatch, _blocks_message([thinking], "max_tokens"))

    assert _reassess_cli(artifact_path, template) == 1

    call = _saved_reassessment(artifact_path)["call_diagnostics"]
    assert call["response_received"] is True
    assert call["content_blocks"] == {"thinking": 1}
    assert call["text_characters"] == 0
    assert call["parse_text_is_valid_json"] is None
    err = capsys.readouterr().err
    assert "blocks [thinking x1], text 0 chars, valid JSON syntax: n/a (no text)" in err
    assert "THINKING-MARKER" not in err


def test_api_failure_leaves_response_fields_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact_path, template = _answered(tmp_path, monkeypatch)
    _install(monkeypatch, _api_error())
    _fixed_clock(monkeypatch, 5.0, 5.75)

    assert _reassess_cli(artifact_path, template) == 1

    saved = _saved_reassessment(artifact_path)
    assert saved["elapsed_seconds"] == 0.75
    assert saved["call_diagnostics"] == {
        "requested_model": _MODEL,
        "max_output_tokens": clarification.REASSESSMENT_MAX_OUTPUT_TOKENS,
        "elapsed_seconds": 0.75,
        "response_received": False,
        "returned_model": None,
        "stop_reason": None,
        "content_blocks": None,
        "text_characters": None,
        "parse_text_is_valid_json": None,
        "usage": {
            "model_calls": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_incomplete": True,
        },
    }
    err = capsys.readouterr().err
    assert "elapsed 0.8s" in err
    assert "Response: none received" in err
    assert "dummy-test-key" not in err + json.dumps(saved)


def test_failed_question_call_records_elapsed_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _write_run(tmp_path)
    _install(monkeypatch, _message('{"questions": [', "max_tokens"))
    _fixed_clock(monkeypatch, 0.0, 3.5)

    assert clarification.main(["prepare", "--run", str(run_dir)]) == 1

    (directory,) = run_dir.glob("clarify-*")
    generation = json.loads((directory / "clarification.json").read_text())["question_generation"]
    assert generation["elapsed_seconds"] == 3.5
    assert generation["usage"]["input_tokens"] == 800
    assert generation["call_diagnostics"]["stop_reason"] == "max_tokens"
    assert generation["call_diagnostics"]["parse_text_is_valid_json"] is False
    assert generation["call_diagnostics"]["max_output_tokens"] == 15000
