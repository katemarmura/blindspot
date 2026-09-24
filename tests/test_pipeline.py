import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, StopReason, TextBlock, Usage

from blindspot import pipeline
from blindspot.expert import ExpertError, ExpertRun, FailureDiagnostics, UsageSummary
from blindspot.schemas import ExpertReport, Finding

_PLAN = "Open a coffee shop in Warsaw."
_MODEL = "claude-haiku-4-5"

FakeExpert = Callable[[str, str], Awaitable[ExpertRun]]


def _finding(label: str) -> Finding:
    return Finding(
        title=f"{label} finding",
        observation=f"Observation from {label}.",
        why_it_matters=f"Why {label} matters.",
        assumptions=[],
        question_to_check=f"Question from {label}?",
    )


def _run_for(
    perspective: str,
    *,
    findings: int = 1,
    input_tokens: int = 100,
    output_tokens: int = 50,
    recovery: bool = False,
) -> ExpertRun:
    calls = 2 if recovery else 1
    return ExpertRun(
        report=ExpertReport(findings=[_finding(perspective) for _ in range(findings)]),
        model=_MODEL,
        usage=UsageSummary(
            model_calls=calls,
            calls_with_usage=calls,
            recovery_attempted=recovery,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no real .env is ever loaded
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


class _Recorder:
    """A fake `run_expert` that records calls and delegates to a per-perspective handler."""

    def __init__(self, handler: FakeExpert | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._handler = handler

    async def __call__(self, plan: str, perspective: str) -> ExpertRun:
        self.calls.append((plan, perspective))
        if self._handler is not None:
            return await self._handler(plan, perspective)
        return _run_for(perspective)


def _install(monkeypatch: pytest.MonkeyPatch, handler: FakeExpert | None = None) -> _Recorder:
    recorder = _Recorder(handler)
    monkeypatch.setattr(pipeline, "run_expert", recorder)
    return recorder


def _run(perspectives: list[str]) -> pipeline.PipelineResult:
    return asyncio.run(pipeline.run_pipeline(_PLAN, perspectives))


def _cli(perspectives: list[str]) -> list[str]:
    argv = ["--plan", _PLAN]
    for p in perspectives:
        argv += ["--perspective", p]
    return argv


# --- Input validation ---


@pytest.mark.parametrize(
    ("plan", "perspectives", "message"),
    [
        pytest.param("  ", ["Financial"], "plan", id="blank-plan"),
        pytest.param(_PLAN, [], "between 1 and 3", id="no-perspectives"),
        pytest.param(_PLAN, ["A", "B", "C", "D"], "between 1 and 3", id="too-many"),
        pytest.param(_PLAN, ["Financial", "  "], "blank", id="blank-perspective"),
        pytest.param(_PLAN, ["Financial", " financial "], "duplicate", id="case-duplicate"),
    ],
)
def test_invalid_input_is_rejected_before_any_expert_starts(
    monkeypatch: pytest.MonkeyPatch, plan: str, perspectives: list[str], message: str
) -> None:
    recorder = _install(monkeypatch)

    with pytest.raises(ValueError, match=message):
        asyncio.run(pipeline.run_pipeline(plan, perspectives))

    assert recorder.calls == []


def test_missing_configuration_is_rejected_before_any_expert_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    recorder = _install(monkeypatch)

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        _run(["Financial"])

    assert recorder.calls == []


def test_perspectives_are_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install(monkeypatch)

    result = _run(["  Financial ", "Operations"])

    assert [o.perspective for o in result.outcomes] == ["Financial", "Operations"]
    assert sorted(p for _, p in recorder.calls) == ["Financial", "Operations"]


# --- Concurrency, isolation, ordering ---


def test_experts_overlap_in_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    started = 0
    all_started = asyncio.Event()

    async def handler(plan: str, perspective: str) -> ExpertRun:
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        # Each expert blocks until all three are running at once; run
        # sequentially, the first would wait forever (bounded by the timeout).
        await asyncio.wait_for(all_started.wait(), timeout=5)
        return _run_for(perspective)

    _install(monkeypatch, handler)

    result = _run(["Financial", "Operations", "Legal"])

    assert result.status == "success"


def test_each_expert_gets_the_same_plan_and_only_its_own_perspective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install(monkeypatch)

    _run(["Financial", "Operations", "Legal"])

    assert sorted(recorder.calls) == sorted(
        [(_PLAN, "Financial"), (_PLAN, "Operations"), (_PLAN, "Legal")]
    )


def test_results_keep_input_order_when_completion_order_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["Financial", "Operations", "Legal"]
    release = {name: asyncio.Event() for name in names}
    completed: list[str] = []

    async def handler(plan: str, perspective: str) -> ExpertRun:
        await asyncio.wait_for(release[perspective].wait(), timeout=5)
        completed.append(perspective)
        # Release the expert listed before this one, so they finish in reverse.
        index = names.index(perspective)
        if index > 0:
            release[names[index - 1]].set()
        return _run_for(perspective)

    async def run() -> pipeline.PipelineResult:
        task = asyncio.create_task(pipeline.run_pipeline(_PLAN, names))
        await asyncio.sleep(0)
        release["Legal"].set()
        return await task

    _install(monkeypatch, handler)

    result = asyncio.run(run())

    assert completed == ["Legal", "Operations", "Financial"]
    assert [o.perspective for o in result.outcomes] == names
    assert [o.run.report.findings[0].title for o in result.outcomes if o.run] == [
        "Financial finding",
        "Operations finding",
        "Legal finding",
    ]


# --- Outcomes and status ---


def test_one_expected_failure_preserves_other_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(plan: str, perspective: str) -> ExpertRun:
        if perspective == "Operations":
            raise ExpertError(
                "The model declined to respond to this request.",
                UsageSummary(model_calls=1, calls_with_usage=1, input_tokens=80, output_tokens=5),
            )
        return _run_for(perspective)

    _install(monkeypatch, handler)

    result = _run(["Financial", "Operations", "Legal"])

    assert result.status == "partial"
    assert [o.status for o in result.outcomes] == ["success", "failed", "success"]
    failed = result.outcomes[1]
    assert failed.run is None
    assert failed.error == "The model declined to respond to this request."
    assert failed.usage is not None and failed.usage.input_tokens == 80
    for ok in (result.outcomes[0], result.outcomes[2]):
        assert ok.run is not None and ok.error is None
        assert ok.run.report.findings == [_finding(ok.perspective)]
        assert ok.elapsed_seconds >= 0


def test_all_failures_give_failed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(plan: str, perspective: str) -> ExpertRun:
        raise ExpertError("Authentication error: invalid x-api-key", UsageSummary(model_calls=1))

    _install(monkeypatch, handler)

    result = _run(["Financial", "Operations"])

    assert result.status == "failed"
    assert all(o.status == "failed" for o in result.outcomes)


def test_empty_findings_count_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(plan: str, perspective: str) -> ExpertRun:
        return _run_for(perspective, findings=0)

    _install(monkeypatch, handler)

    result = _run(["Financial", "Operations"])

    assert result.status == "success"
    assert all(o.run is not None and o.run.report.findings == [] for o in result.outcomes)


def test_unexpected_errors_are_not_disguised_as_expert_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[str] = []

    async def handler(plan: str, perspective: str) -> ExpertRun:
        if perspective == "Operations":
            raise RuntimeError("bug")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(perspective)
            raise
        return _run_for(perspective)

    _install(monkeypatch, handler)

    with pytest.raises(ExceptionGroup) as exc_info:
        _run(["Financial", "Operations", "Legal"])

    assert exc_info.group_contains(RuntimeError, match="bug")
    assert sorted(cancelled) == ["Financial", "Legal"]


def test_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(plan: str, perspective: str) -> ExpertRun:
        await asyncio.sleep(10)
        return _run_for(perspective)

    _install(monkeypatch, handler)

    async def run() -> None:
        task = asyncio.create_task(pipeline.run_pipeline(_PLAN, ["Financial", "Operations"]))
        await asyncio.sleep(0)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


# --- CLI ---


def _mixed_handler() -> FakeExpert:
    async def handler(plan: str, perspective: str) -> ExpertRun:
        if perspective == "Financial":
            return _run_for(perspective, input_tokens=1000, output_tokens=1300, recovery=True)
        if perspective == "Operations":
            # Recovery call hit an API error: usage for 1 of 2 calls is unknown.
            raise ExpertError(
                "Authentication error: invalid x-api-key",
                UsageSummary(
                    model_calls=2,
                    calls_with_usage=1,
                    recovery_attempted=True,
                    input_tokens=900,
                    output_tokens=1200,
                ),
                returned_model=_MODEL,  # the initial response did arrive
                diagnostics=FailureDiagnostics(failure_stage="selection"),
            )
        return _run_for(perspective, input_tokens=100, output_tokens=50)

    return handler


def test_cli_partial_prints_ordered_json_and_aggregated_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, _mixed_handler())

    exit_code = pipeline.main(_cli(["Financial", "Operations", "Legal"]))

    assert exit_code != 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["status"] == "partial"
    assert [e["perspective"] for e in data["experts"]] == ["Financial", "Operations", "Legal"]
    assert [e["status"] for e in data["experts"]] == ["success", "failed", "success"]

    financial, operations, legal = data["experts"]
    assert financial["model"] == _MODEL
    assert financial["report"] == {"findings": [_finding("Financial").model_dump()]}
    assert financial["error"] is None
    assert financial["usage"] == {
        "model_calls": 2,
        "recovery_attempted": True,
        "input_tokens": 1000,
        "output_tokens": 1300,
        "usage_incomplete": False,
    }
    assert operations["report"] is None
    assert operations["model"] == _MODEL
    assert operations["diagnostics"] == {
        "failure_stage": "selection",
        "stop_reason": None,
        "recovery_skip_reason": None,
        "validation_errors": None,
    }
    assert financial["diagnostics"] is None
    assert operations["error"] == "Authentication error: invalid x-api-key"
    assert operations["usage"]["usage_incomplete"] is True
    assert legal["usage"]["model_calls"] == 1
    assert all(isinstance(e["elapsed_seconds"], float) for e in data["experts"])

    assert "Status: partial (2/3 experts succeeded)" in out.err
    assert "Model calls: 5 (recoveries: 2)" in out.err
    assert "Input tokens: 2000 (incomplete: usage unavailable for 1 of 5 calls)" in out.err
    assert "Output tokens: 2550 (incomplete" in out.err
    assert "Elapsed:" in out.err
    assert "dummy-test-key" not in out.out + out.err


def test_cli_all_success_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch)

    assert pipeline.main(_cli(["Financial", "Operations"])) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == "success"
    assert "Model calls: 2 (recoveries: 0)" in out.err
    assert "Input tokens: 200\n" in out.err
    assert "incomplete" not in out.err


def test_cli_all_failed_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def handler(plan: str, perspective: str) -> ExpertRun:
        raise ExpertError("Connection error: boom", UsageSummary(model_calls=1))

    _install(monkeypatch, handler)

    assert pipeline.main(_cli(["Financial"])) != 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["status"] == "failed"
    assert data["experts"][0]["model"] is None  # no response was received
    assert data["experts"][0]["usage"]["usage_incomplete"] is True
    assert "Input tokens: 0 (incomplete: usage unavailable for 1 of 1 calls)" in out.err


def test_cli_invalid_input_exits_nonzero_without_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _install(monkeypatch)

    assert pipeline.main(_cli(["Financial", "FINANCIAL"])) != 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "duplicate perspective" in out.err
    assert recorder.calls == []


# --- End to end through the real run_expert, with a fake Anthropic client ---


class _FakeMessages:
    """Responds per perspective; responses report a dated model ID."""

    def __init__(self, stop_reasons: dict[str, StopReason]) -> None:
        self._stop_reasons = stop_reasons

    async def create(self, **kwargs: Any) -> Message:
        content = kwargs["messages"][0]["content"]
        perspective = content.rsplit("Perspective:\n", 1)[1]
        stop_reason = self._stop_reasons[perspective]
        text = (
            json.dumps({"findings": [_finding(perspective).model_dump()]})
            if stop_reason == "end_turn"
            else '{"findings": ['
        )
        return Message(
            id="msg_test",
            type="message",
            role="assistant",
            model=f"{kwargs['model']}-20260101",
            content=[TextBlock(type="text", text=text)],
            stop_reason=stop_reason,
            stop_sequence=None,
            usage=Usage(input_tokens=10, output_tokens=5),
        )


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def test_cli_truncated_expert_keeps_returned_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: a failed expert used to report "model": null even when a
    # response (here, a truncated one) had been received.
    messages = _FakeMessages({"Financial": "end_turn", "Operations": "max_tokens"})
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))

    assert pipeline.main(_cli(["Financial", "Operations"])) != 0

    out = capsys.readouterr().out
    financial, operations = json.loads(out)["experts"]
    assert financial["status"] == "success"
    assert financial["model"] == f"{_MODEL}-20260101"
    assert operations["status"] == "failed"
    assert "truncated" in operations["error"]
    assert operations["model"] == f"{_MODEL}-20260101"
    assert operations["report"] is None
    assert '{"findings": [' not in out
    assert operations["diagnostics"] == {
        "failure_stage": "initial",
        "stop_reason": "max_tokens",
        "recovery_skip_reason": "response_not_end_turn",
        "validation_errors": None,
    }
    assert financial["diagnostics"] is None
