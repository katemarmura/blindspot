import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import ContentBlock, Message, StopReason, TextBlock, Usage

from blindspot import compare
from blindspot.expert import ExpertError, ExpertRun, UsageSummary
from blindspot.prompts import EXPERT_SYSTEM_PROMPT, SELECTION_SYSTEM_PROMPT
from blindspot.schemas import ExpertReport, Finding

_PLAN = "Open a coffee shop in Warsaw."
_PERSPECTIVE = "Operations"
_MODEL_A = "model-a"
_MODEL_B = "model-b"
_KEY = "dummy-test-key"

Handler = Callable[[str, str, str], Awaitable[ExpertRun]]


def _finding(model: str) -> Finding:
    return Finding(
        title=f"Finding from {model}",
        observation="Observation.",
        why_it_matters="Why it matters.",
        assumptions=[],
        question_to_check="Question?",
    )


def _run_for(
    model: str, *, recovery: bool = False, input_tokens: int = 100, output_tokens: int = 50
) -> ExpertRun:
    calls = 2 if recovery else 1
    return ExpertRun(
        report=ExpertReport(findings=[_finding(model)]),
        model=f"{model}-20260101",
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
    monkeypatch.chdir(tmp_path)  # no real .env is loaded; runs/ lands in tmp_path
    monkeypatch.setenv("ANTHROPIC_API_KEY", _KEY)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)  # overrides must not need it
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


class _Recorder:
    def __init__(self, handler: Handler | None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._handler = handler

    async def __call__(self, plan: str, perspective: str, *, model: str | None = None) -> ExpertRun:
        self.calls.append((plan, perspective, model))
        assert model is not None
        if self._handler is not None:
            return await self._handler(plan, perspective, model)
        return _run_for(model)


def _install(monkeypatch: pytest.MonkeyPatch, handler: Handler | None = None) -> _Recorder:
    recorder = _Recorder(handler)
    monkeypatch.setattr(compare, "run_expert", recorder)
    return recorder


def _argv(
    *,
    plan: str = _PLAN,
    perspective: str = _PERSPECTIVE,
    models: tuple[str, ...] = (_MODEL_A, _MODEL_B),
    repeats: int | None = None,
) -> list[str]:
    argv = ["--plan", plan, "--perspective", perspective]
    for model in models:
        argv += ["--model", model]
    if repeats is not None:
        argv += ["--repeats", str(repeats)]
    return argv


def _comparison_dir(tmp_path: Path) -> Path:
    (directory,) = (tmp_path / "runs").iterdir()
    return directory


def _run_files(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("run-*.json"))]


# --- Validation ---


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(_argv(plan="  "), "plan", id="blank-plan"),
        pytest.param(_argv(perspective=" "), "perspective", id="blank-perspective"),
        pytest.param(_argv(models=(_MODEL_A,)), "exactly two", id="one-model"),
        pytest.param(_argv(models=(_MODEL_A, _MODEL_B, "model-c")), "exactly two", id="three"),
        pytest.param(_argv(models=(_MODEL_A, " ")), "blank", id="blank-model"),
        pytest.param(_argv(models=(_MODEL_A, f" {_MODEL_A} ")), "different", id="same-model"),
        pytest.param(_argv(repeats=0), "between 1 and 3", id="zero-repeats"),
        pytest.param(_argv(repeats=4), "between 1 and 3", id="four-repeats"),
    ],
)
def test_invalid_arguments_are_rejected_before_any_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    recorder = _install(monkeypatch)

    assert compare.main(argv) == 1

    assert message in capsys.readouterr().err
    assert recorder.calls == []
    assert not (tmp_path / "runs").exists()


def test_missing_api_key_is_rejected_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    recorder = _install(monkeypatch)

    assert compare.main(_argv()) == 1

    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert recorder.calls == []
    assert not (tmp_path / "runs").exists()


# --- Ordering and independence ---


def test_default_is_one_repetition_per_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install(monkeypatch)

    assert compare.main(_argv()) == 0

    assert [model for _, _, model in recorder.calls] == [_MODEL_A, _MODEL_B]


def test_models_alternate_first_position_across_repetitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install(monkeypatch)

    assert compare.main(_argv(repeats=3)) == 0

    assert [model for _, _, model in recorder.calls] == [
        _MODEL_A,
        _MODEL_B,
        _MODEL_B,
        _MODEL_A,
        _MODEL_A,
        _MODEL_B,
    ]
    saved = _run_files(_comparison_dir(tmp_path))
    assert [(r["execution_order"], r["repetition"], r["requested_model"]) for r in saved] == [
        (1, 1, _MODEL_A),
        (2, 1, _MODEL_B),
        (3, 2, _MODEL_B),
        (4, 2, _MODEL_A),
        (5, 3, _MODEL_A),
        (6, 3, _MODEL_B),
    ]


def test_every_run_gets_identical_plan_and_perspective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install(monkeypatch)

    compare.main(_argv(repeats=2))

    assert len(recorder.calls) == 4
    assert {(plan, perspective) for plan, perspective, _ in recorder.calls} == {
        (_PLAN, _PERSPECTIVE)
    }


def test_runs_are_sequential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    max_active = 0

    async def handler(plan: str, perspective: str, model: str) -> ExpertRun:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return _run_for(model)

    _install(monkeypatch, handler)

    compare.main(_argv(repeats=2))

    assert max_active == 1


# --- Saved results ---


def test_manifest_records_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)

    compare.main(_argv(repeats=2))

    manifest = json.loads((_comparison_dir(tmp_path) / "manifest.json").read_text())
    assert manifest["plan"] == _PLAN
    assert manifest["perspective"] == _PERSPECTIVE
    assert manifest["models"] == [_MODEL_A, _MODEL_B]
    assert manifest["repeats"] == 2
    assert manifest["created_at"].endswith("+00:00")
    assert manifest["prompts"] == {
        "expert_system": EXPERT_SYSTEM_PROMPT,
        "selection_system": SELECTION_SYSTEM_PROMPT,
    }
    assert set(manifest["output_schemas"]) == {"expert_report", "selection"}
    assert manifest["request_limits"]["expert_max_output_tokens"] == 3000
    assert manifest["request_limits"]["selection_max_output_tokens"] == 256
    # tmp_path is not a git repository: unknown, not invented.
    assert manifest["git"] == {"revision": None, "dirty": None}


def test_successful_and_failed_runs_are_saved_individually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def handler(plan: str, perspective: str, model: str) -> ExpertRun:
        if model == _MODEL_B:
            raise ExpertError(
                "The model declined to respond to this request.",
                UsageSummary(model_calls=1, calls_with_usage=1, input_tokens=80, output_tokens=3),
            )
        return _run_for(model)

    _install(monkeypatch, handler)

    assert compare.main(_argv()) == 1

    ok, failed = _run_files(_comparison_dir(tmp_path))
    assert ok["status"] == "success"
    assert ok["returned_model"] == f"{_MODEL_A}-20260101"
    assert ok["report"] == {"findings": [_finding(_MODEL_A).model_dump()]}
    assert ok["error"] is None
    assert ok["elapsed_seconds"] >= 0
    assert ok["usage"] == {
        "model_calls": 1,
        "recovery_attempted": False,
        "input_tokens": 100,
        "output_tokens": 50,
        "usage_incomplete": False,
    }
    assert failed["status"] == "failed"
    assert failed["requested_model"] == _MODEL_B
    assert failed["returned_model"] is None
    assert failed["report"] is None
    assert failed["error"] == "The model declined to respond to this request."
    assert failed["usage"]["input_tokens"] == 80


@pytest.mark.parametrize("interruption", [RuntimeError("bug"), asyncio.CancelledError()])
def test_earlier_results_survive_a_later_unexpected_error_or_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: BaseException
) -> None:
    async def handler(plan: str, perspective: str, model: str) -> ExpertRun:
        if len(recorder.calls) == 3:
            raise interruption
        return _run_for(model)

    recorder = _install(monkeypatch, handler)

    with pytest.raises(type(interruption)):
        compare.main(_argv(repeats=2))

    directory = _comparison_dir(tmp_path)
    assert (directory / "manifest.json").exists()
    assert [r["execution_order"] for r in _run_files(directory)] == [1, 2]
    assert list(directory.glob("*.tmp")) == []


def test_expected_failures_do_not_stop_later_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(plan: str, perspective: str, model: str) -> ExpertRun:
        if len(recorder.calls) == 1:
            raise ExpertError("Connection error: boom", UsageSummary(model_calls=1))
        return _run_for(model)

    recorder = _install(monkeypatch, handler)

    assert compare.main(_argv(repeats=2)) == 1

    assert [r["status"] for r in _run_files(_comparison_dir(tmp_path))] == [
        "failed",
        "success",
        "success",
        "success",
    ]


# --- Summary ---


def test_summary_reports_recovery_and_incomplete_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def handler(plan: str, perspective: str, model: str) -> ExpertRun:
        if model == _MODEL_A:
            return _run_for(model, recovery=True, input_tokens=1000, output_tokens=1300)
        if len(recorder.calls) == 2:
            # Recovery call failed with an API error: one call's usage is unknown.
            raise ExpertError(
                "Authentication error: invalid x-api-key",
                UsageSummary(
                    model_calls=2,
                    calls_with_usage=1,
                    recovery_attempted=True,
                    input_tokens=900,
                    output_tokens=1200,
                ),
            )
        return _run_for(model, input_tokens=100, output_tokens=50)

    recorder = _install(monkeypatch, handler)

    assert compare.main(_argv(repeats=2)) == 1

    out = capsys.readouterr().out
    directory = _comparison_dir(tmp_path)
    assert (
        out
        == "\n".join(
            [
                f"{_MODEL_A}:",
                "  runs: 2 succeeded, 0 failed",
                "  runs requiring recovery: 2",
                "  input tokens: 2000",
                "  output tokens: 2600",
                out.splitlines()[5],  # average elapsed, timing-dependent
                f"{_MODEL_B}:",
                "  runs: 1 succeeded, 1 failed",
                "  runs requiring recovery: 1",
                "  input tokens: 1000 (incomplete)",
                "  output tokens: 1250 (incomplete)",
                out.splitlines()[11],
                f"Results saved in: {directory.relative_to(tmp_path)}",
            ]
        )
        + "\n"
    )
    assert out.splitlines()[5].startswith("  average elapsed: ")
    saved = _run_files(directory)
    assert saved[1]["usage"]["usage_incomplete"] is True


# --- End to end through the real run_expert, with a fake Anthropic client ---


_Outcome = tuple[str | None, StopReason] | BaseException


class _FakeMessages:
    """Serves one queued outcome per call; responses report a dated model ID."""

    def __init__(self, outcomes: list[_Outcome]) -> None:
        self._outcomes = list(outcomes)

    async def create(self, **kwargs: Any) -> Message:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        text, stop_reason = outcome
        content: list[ContentBlock] = [] if text is None else [TextBlock(type="text", text=text)]
        return Message(
            id="msg_test",
            type="message",
            role="assistant",
            model=f"{kwargs['model']}-20260101",
            content=content,
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


def _install_client(monkeypatch: pytest.MonkeyPatch, *outcomes: _Outcome) -> None:
    messages = _FakeMessages(list(outcomes))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))


def _auth_error() -> anthropic.AuthenticationError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        401,
        request=request,
        json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
    )
    return anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)


_VALID_TEXT = json.dumps({"findings": [_finding(_MODEL_A).model_dump()]})


def test_saved_files_contain_no_credentials_or_raw_invalid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "RAW-INVALID-OUTPUT-MARKER"
    invalid = json.dumps({"findings": [], "leaked_field": marker})
    _install_client(monkeypatch, (_VALID_TEXT, "end_turn"), (invalid, "end_turn"))

    assert compare.main(_argv()) == 1

    directory = _comparison_dir(tmp_path)
    saved_text = "".join(p.read_text() for p in directory.iterdir())
    assert _KEY not in saved_text
    assert marker not in saved_text
    assert "x-api-key" not in saved_text.lower()
    ok, failed = _run_files(directory)
    assert ok["returned_model"] == f"{_MODEL_A}-20260101"
    assert failed["status"] == "failed"
    assert failed["returned_model"] == f"{_MODEL_B}-20260101"
    assert "leaked_field" not in saved_text
    assert "<unknown field>: Extra inputs are not permitted" in failed["error"]
    assert failed["diagnostics"]["validation_errors"] == [
        {"schema": "expert_report", "loc": "<unknown field>", "type": "extra_forbidden"},
        {"schema": "candidate_report", "loc": "<unknown field>", "type": "extra_forbidden"},
    ]


def test_truncated_run_keeps_returned_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a failed run used to save returned_model as null even when a
    # response (here, a truncated one) had been received.
    _install_client(
        monkeypatch,
        ('{"findings": [', "max_tokens"),
        (_VALID_TEXT, "end_turn"),
    )

    assert compare.main(_argv()) == 1

    truncated, ok = _run_files(_comparison_dir(tmp_path))
    assert truncated["status"] == "failed"
    assert "truncated" in truncated["error"]
    assert truncated["requested_model"] == _MODEL_A
    assert truncated["returned_model"] == f"{_MODEL_A}-20260101"
    assert truncated["report"] is None
    assert '{"findings": [' not in json.dumps(truncated)
    assert truncated["diagnostics"] == {
        "failure_stage": "initial",
        "stop_reason": "max_tokens",
        "recovery_skip_reason": "response_not_end_turn",
        "validation_errors": None,
    }
    assert ok["returned_model"] == f"{_MODEL_B}-20260101"
    assert ok["diagnostics"] is None


def test_returned_model_is_null_when_no_response_was_received(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_client(monkeypatch, _auth_error(), (_VALID_TEXT, "end_turn"))

    assert compare.main(_argv()) == 1

    failed, _ = _run_files(_comparison_dir(tmp_path))
    assert failed["status"] == "failed"
    assert failed["returned_model"] is None
    assert failed["usage"]["usage_incomplete"] is True
    assert failed["diagnostics"]["stop_reason"] is None
    assert failed["diagnostics"]["recovery_skip_reason"] == "no_response"


def test_saved_run_keeps_candidate_validation_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = [_finding(_MODEL_A).model_dump() for _ in range(4)]
    findings[2]["title"] = " "
    _install_client(
        monkeypatch,
        (json.dumps({"findings": findings}), "end_turn"),
        (_VALID_TEXT, "end_turn"),
    )

    assert compare.main(_argv()) == 1

    failed, _ = _run_files(_comparison_dir(tmp_path))
    assert failed["usage"]["recovery_attempted"] is False
    assert failed["diagnostics"] == {
        "failure_stage": "initial",
        "stop_reason": "end_turn",
        "recovery_skip_reason": "candidate_validation_failed",
        "validation_errors": [
            {"schema": "expert_report", "loc": "findings", "type": "too_long"},
            {"schema": "candidate_report", "loc": "findings.2.title", "type": "value_error"},
        ],
    }
