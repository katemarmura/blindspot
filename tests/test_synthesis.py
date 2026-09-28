import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pydantic
import pytest
from anthropic.types import ContentBlock, Message, StopReason, TextBlock, Usage

from blindspot import synthesis
from blindspot.prompts import SYNTHESIS_SYSTEM_PROMPT
from blindspot.schemas import (
    ExpertDefinition,
    ExpertReport,
    Finding,
    SynthesisReport,
    SynthesizedFinding,
)
from blindspot.synthesis import ExpertInput

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
        perspective=perspective,
        objective=f"{perspective} objective.",
        questions=[f"{perspective} question?"],
    )


# Team of four positions: e1 two findings, e2 failed, e3 empty report, e4 one finding.
_EXPERTS = [
    ExpertInput(1, _assignment("Financial"), ExpertReport(findings=[_finding("A"), _finding("B")])),
    ExpertInput(2, _assignment("Legal & Regulatory"), None),
    ExpertInput(3, _assignment("Technical"), ExpertReport(findings=[])),
    ExpertInput(4, _assignment(" Coffee Supply "), ExpertReport(findings=[_finding("C")])),
]


def _synth(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "Cash buffer",
        "observation": "If revenue ramps slowly, reserves may matter.",
        "why_it_matters": "It affects how long the shop can operate.",
        "assumptions": ["Revenue ramps slowly"],
        "question_to_check": "How many months can reserves cover?",
        "source_finding_ids": ["e1-f1"],
        **overrides,
    }


# --- Schema ---


def test_valid_synthesis_report() -> None:
    report = SynthesisReport.model_validate(
        {"findings": [_synth(), _synth(source_finding_ids=["e1-f2", "e4-f1"], assumptions=[])]}
    )
    assert report.findings[1].source_finding_ids == ["e1-f2", "e4-f1"]


def test_empty_synthesis_report_is_valid() -> None:
    assert SynthesisReport.model_validate({"findings": []}).findings == []


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"findings": [_synth() for _ in range(6)]}, id="six-findings"),
        pytest.param({"findings": [_synth(source_finding_ids=[])]}, id="no-sources"),
        pytest.param(
            {"findings": [_synth(source_finding_ids=["e1-f1", "e1-f1"])]}, id="duplicate-source"
        ),
        pytest.param({"findings": [_synth(source_finding_ids=[" "])]}, id="blank-source"),
        pytest.param({"findings": [_synth(title=" ")]}, id="blank-title"),
        pytest.param({"findings": [_synth(assumptions=["ok", ""])]}, id="blank-assumption"),
        pytest.param({"findings": [_synth(confidence=0.9)]}, id="extra-finding-field"),
        pytest.param({"findings": [], "summary": "x"}, id="extra-report-field"),
        pytest.param(
            {"findings": [{k: v for k, v in _synth().items() if k != "question_to_check"}]},
            id="missing-field",
        ),
    ],
)
def test_invalid_synthesis_report_is_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        SynthesisReport.model_validate(data)


def test_synthesized_finding_is_its_own_type() -> None:
    assert set(SynthesizedFinding.model_fields) == {*Finding.model_fields, "source_finding_ids"}


# --- Source IDs and input ---


def test_source_ids_are_stable_and_resolve_to_original_findings() -> None:
    sources = synthesis.source_map(_EXPERTS)

    # Team position is kept even though e2 failed and e3 was empty.
    assert list(sources) == ["e1-f1", "e1-f2", "e4-f1"]
    assert sources["e1-f2"].finding is _EXPERTS[0].report.findings[1]  # type: ignore[union-attr]
    assert sources["e4-f1"].to_json() == {
        "expert_position": 4,
        "perspective": "Coffee Supply",
        "finding_position": 1,
        "title": "C title",
    }
    assert synthesis.source_map(_EXPERTS) == sources  # deterministic


def test_input_contains_plan_assignments_ids_and_missing_coverage() -> None:
    content = synthesis.build_input(_PLAN, _EXPERTS)

    assert content.startswith(f"Plan:\n{_PLAN}\n\n")
    assert content == synthesis.build_input(_PLAN, _EXPERTS)  # deterministic
    completed_json, unavailable_json = (
        part.split(":\n", 1)[1] for part in content.split("\n\n", 1)[1].split("\n\nFailed ")
    )
    completed = json.loads(completed_json)
    unavailable = json.loads(unavailable_json)

    assert [e["expert"] for e in completed] == ["e1", "e3", "e4"]
    assert completed[0] == {
        "expert": "e1",
        "perspective": "Financial",
        "objective": "Financial objective.",
        "questions": ["Financial question?"],
        "status": "completed",
        "findings": [
            {"id": "e1-f1", **_finding("A").model_dump()},
            {"id": "e1-f2", **_finding("B").model_dump()},
        ],
    }
    assert completed[1]["findings"] == []  # investigated, nothing found
    assert unavailable == [
        {
            "expert": "e2",
            "perspective": "Legal & Regulatory",
            "objective": "Legal & Regulatory objective.",
            "questions": ["Legal & Regulatory question?"],
            "status": "unavailable",
        }
    ]
    assert "missing coverage, not evidence" in content


def test_building_input_does_not_modify_reports() -> None:
    before = [e.report.model_dump() if e.report else None for e in _EXPERTS]
    synthesis.build_input(_PLAN, _EXPERTS)
    synthesis.source_map(_EXPERTS)
    assert [e.report.model_dump() if e.report else None for e in _EXPERTS] == before


# --- Synthesis call (fake client) ---


@pytest.fixture
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


def _message(text: str | None, stop_reason: StopReason = "end_turn") -> Message:
    content: list[ContentBlock] = [] if text is None else [TextBlock(type="text", text=text)]
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=f"{_MODEL}-20260101",
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=900, output_tokens=300),
    )


class _FakeMessages:
    def __init__(self, outcome: Message | BaseException) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        assert len(self.calls) == 1, "synthesis must never make a second call"
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _install(
    monkeypatch: pytest.MonkeyPatch, outcome: Message | BaseException
) -> tuple[_FakeMessages, list[dict[str, object]]]:
    messages = _FakeMessages(outcome)
    clients: list[dict[str, object]] = []

    def factory(**kwargs: object) -> _FakeClient:
        clients.append(kwargs)
        return _FakeClient(messages)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
    return messages, clients


def _run() -> synthesis.SynthesisRun:
    return asyncio.run(synthesis.synthesize(_PLAN, _EXPERTS))


@pytest.mark.usefixtures("_env")
def test_synthesize_makes_one_structured_call(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"findings": [_synth(source_finding_ids=["e1-f2", "e4-f1"])]}
    messages, clients = _install(monkeypatch, _message(json.dumps(report)))

    run = _run()

    assert run.report.findings[0].source_finding_ids == ["e1-f2", "e4-f1"]
    assert run.model == f"{_MODEL}-20260101"
    assert (run.usage.model_calls, run.usage.input_tokens, run.usage.output_tokens) == (1, 900, 300)
    (call,) = messages.calls
    assert call["model"] == _MODEL
    assert call["max_tokens"] == 3000
    assert call["system"] == SYNTHESIS_SYSTEM_PROMPT
    assert call["messages"][0]["content"] == synthesis.build_input(_PLAN, _EXPERTS)
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert clients[0]["timeout"] == 60.0
    assert clients[0]["max_retries"] == 0


@pytest.mark.usefixtures("_env")
def test_unknown_source_ids_are_a_failure_without_leaking_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "e9-EVIL-MARKER"
    report = {"findings": [_synth(), _synth(source_finding_ids=["e1-f2", marker])]}
    _install(monkeypatch, _message(json.dumps(report)))

    with pytest.raises(synthesis.SynthesisError, match="unknown source finding ID") as exc_info:
        _run()

    error = exc_info.value
    assert error.returned_model == f"{_MODEL}-20260101"
    assert error.usage is not None and error.usage.input_tokens == 900
    assert error.diagnostics is not None
    diagnostics = error.diagnostics.to_json()
    assert diagnostics["failure_stage"] == "synthesis"
    assert diagnostics["stop_reason"] == "end_turn"
    assert diagnostics["validation_errors"] == [
        {
            "schema": "synthesis_report",
            "loc": "findings.1.source_finding_ids.1",
            "type": "unknown_source_id",
        }
    ]
    assert marker not in str(error) + json.dumps(diagnostics)


@pytest.mark.usefixtures("_env")
def test_ids_of_failed_or_empty_experts_are_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {
        "findings": [_synth(source_finding_ids=["e2-f1"]), _synth(source_finding_ids=["e3-f1"])]
    }
    _install(monkeypatch, _message(json.dumps(report)))

    with pytest.raises(synthesis.SynthesisError) as exc_info:
        _run()

    assert exc_info.value.diagnostics is not None
    issues = exc_info.value.diagnostics.validation_errors
    assert issues is not None and len(issues) == 2


@pytest.mark.usefixtures("_env")
@pytest.mark.parametrize(
    ("response", "stop_reason", "match", "issue"),
    [
        pytest.param(_message(None, "refusal"), "refusal", "declined", None, id="refusal"),
        pytest.param(
            _message('{"findings": [', "max_tokens"), "max_tokens", "truncated", None, id="trunc"
        ),
        pytest.param(_message(None), "end_turn", "no output", None, id="no-text"),
        pytest.param(
            _message("not json"), "end_turn", "invalid", ("<root>", "json_invalid"), id="malformed"
        ),
        pytest.param(
            _message(json.dumps({"findings": [_synth() for _ in range(6)]})),
            "end_turn",
            "invalid",
            ("findings", "too_long"),
            id="six-findings",
        ),
        pytest.param(
            _message(json.dumps({"findings": [_synth(source_finding_ids=["e1-f1", "e1-f1"])]})),
            "end_turn",
            "invalid",
            ("findings.0.source_finding_ids", "value_error"),
            id="duplicate-sources",
        ),
    ],
)
def test_synthesis_failures_are_diagnosed(
    monkeypatch: pytest.MonkeyPatch,
    response: Message,
    stop_reason: str,
    match: str,
    issue: tuple[str, str] | None,
) -> None:
    messages, _ = _install(monkeypatch, response)

    with pytest.raises(synthesis.SynthesisError, match=match) as exc_info:
        _run()

    assert len(messages.calls) == 1  # no retry, no repair
    error = exc_info.value
    assert error.returned_model == f"{_MODEL}-20260101"
    assert error.diagnostics is not None
    assert error.diagnostics.failure_stage == "synthesis"
    assert error.diagnostics.stop_reason == stop_reason
    if issue is None:
        assert error.diagnostics.validation_errors is None
    else:
        assert error.diagnostics.validation_errors is not None
        assert (issue[0], issue[1]) in {
            (i.loc, i.type) for i in error.diagnostics.validation_errors
        }


@pytest.mark.usefixtures("_env")
def test_synthesis_api_error_records_null_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        401,
        request=request,
        json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
    )
    _install(
        monkeypatch,
        anthropic.AuthenticationError("invalid x-api-key", response=response, body=None),
    )

    with pytest.raises(synthesis.SynthesisError, match="Authentication error") as exc_info:
        _run()

    error = exc_info.value
    assert error.returned_model is None
    assert error.usage is not None and error.usage.calls_missing_usage == 1
    assert error.diagnostics is not None
    assert error.diagnostics.to_json() == {
        "failure_stage": "synthesis",
        "stop_reason": None,
        "recovery_skip_reason": None,
        "validation_errors": None,
    }
    assert "dummy-test-key" not in str(error)


@pytest.mark.usefixtures("_env")
def test_unknown_fields_do_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = "EVIL-SYNTH-KEY"
    report = {"findings": [_synth(**{marker: "x"})], marker: marker}
    _install(monkeypatch, _message(json.dumps(report)))

    with pytest.raises(synthesis.SynthesisError) as exc_info:
        _run()

    assert exc_info.value.diagnostics is not None
    serialized = str(exc_info.value) + json.dumps(exc_info.value.diagnostics.to_json())
    assert marker not in serialized
    assert "<unknown field>" in serialized
