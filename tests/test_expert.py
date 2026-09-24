import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import ContentBlock, Message, StopReason, TextBlock, Usage

from blindspot import expert
from blindspot.prompts import EXPERT_SYSTEM_PROMPT, SELECTION_SYSTEM_PROMPT
from blindspot.schemas import Finding

_VALID_FINDING = Finding(
    title="No cash-flow buffer",
    observation="The plan doesn't mention a reserve for slow months.",
    why_it_matters="A single bad month could force the shop to close early.",
    assumptions=["Revenue varies significantly by season"],
    question_to_check="How many months of expenses could the business cover with no revenue?",
)

_CANDIDATES = [
    Finding(
        title=f"Candidate {i}",
        observation=f"Observation text for candidate {i}.",
        why_it_matters=f"Why candidate {i} matters.",
        assumptions=[f"Assumption for candidate {i}"] if i % 2 else [],
        question_to_check=f"Question for candidate {i}?",
    )
    for i in range(5)
]

_PLAN = "Open a coffee shop in Warsaw."
_PERSPECTIVE = "Financial"
_MODEL = "claude-haiku-4-5"


@pytest.fixture(autouse=True)
def _clean_env() -> Iterator[None]:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)


def _message(
    text: str | None,
    stop_reason: StopReason = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> Message:
    content: list[ContentBlock] = [] if text is None else [TextBlock(type="text", text=text)]
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=_MODEL,
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _report_json(findings: list[Finding], **extra: object) -> str:
    return json.dumps({"findings": [f.model_dump() for f in findings], **extra})


def _selection_message(indices: object, **kwargs: Any) -> Message:
    return _message(json.dumps({"selected_indices": indices}), **kwargs)


def _api_error() -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        401,
        request=request,
        json={
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        },
    )
    return anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)


class _FakeMessages:
    """Returns (or raises) one queued outcome per `create` call, recording each call."""

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


class _FakeAsyncAnthropic:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages

    async def __aenter__(self) -> "_FakeAsyncAnthropic":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _install_fake(
    monkeypatch: pytest.MonkeyPatch, *outcomes: Message | BaseException
) -> _FakeMessages:
    messages = _FakeMessages(list(outcomes))

    def factory(**_: object) -> _FakeAsyncAnthropic:
        return _FakeAsyncAnthropic(messages)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
    return messages


def _run() -> expert.ExpertRun:
    return asyncio.run(expert.run_expert(_PLAN, _PERSPECTIVE))


def _run_cli() -> int:
    return expert.main(["--plan", _PLAN, "--perspective", _PERSPECTIVE])


# --- Input validation and the single-call path ---


def test_run_expert_rejects_blank_plan() -> None:
    with pytest.raises(ValueError, match="plan"):
        asyncio.run(expert.run_expert("   ", "Financial"))


def test_run_expert_rejects_blank_perspective() -> None:
    with pytest.raises(ValueError, match="perspective"):
        asyncio.run(expert.run_expert("Open a coffee shop.", "  "))


def test_run_expert_success_makes_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(monkeypatch, _message(_report_json([_VALID_FINDING])))

    run = _run()

    assert run.report.findings == [_VALID_FINDING]
    assert run.model == _MODEL
    assert run.usage.input_tokens == 100
    assert run.usage.output_tokens == 50
    assert run.usage.model_calls == 1
    assert run.usage.recovery_attempted is False
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == _MODEL
    assert call["max_tokens"] == expert.MAX_OUTPUT_TOKENS
    assert call["system"] == EXPERT_SYSTEM_PROMPT
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_run_expert_success_zero_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(monkeypatch, _message(_report_json([])))

    assert _run().report.findings == []


def test_run_expert_refusal_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(monkeypatch, _message(None, stop_reason="refusal"))

    with pytest.raises(expert.ExpertError, match="declined"):
        _run()
    assert len(fake.calls) == 1


def test_run_expert_truncation_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(monkeypatch, _message('{"findings": [', stop_reason="max_tokens"))

    with pytest.raises(expert.ExpertError, match="truncated"):
        _run()
    assert len(fake.calls) == 1


def test_run_expert_missing_text_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(monkeypatch, _message(None))

    with pytest.raises(expert.ExpertError, match="schema"):
        _run()


def test_run_expert_invalid_output_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(monkeypatch, _message('{"findings": "not-a-list"}'))

    with pytest.raises(expert.ExpertError, match="schema"):
        _run()


def test_cli_invalid_output_keeps_raw_model_content_out_of_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    marker = "SECRET-MARKER-XYZ"
    # Malformed model output: an unexpected field carrying arbitrary text.
    _install_fake(monkeypatch, _message(_report_json([], leaked_field=marker)))

    assert _run_cli() == 1
    err = capsys.readouterr().err
    assert marker not in err
    assert "input_value" not in err
    assert "leaked_field" in err
    assert "Extra inputs are not permitted" in err


def test_run_expert_api_failure_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(monkeypatch, _api_error())

    with pytest.raises(expert.ExpertError, match="Authentication error") as exc_info:
        _run()

    assert "dummy-test-key" not in str(exc_info.value)
    usage = exc_info.value.usage
    assert usage is not None
    assert usage.model_calls == 1
    assert usage.calls_missing_usage == 1


def test_cli_success_prints_json_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(monkeypatch, _message(_report_json([_VALID_FINDING])))

    assert _run_cli() == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["findings"][0]["title"] == "No cash-flow buffer"
    assert f"Model: {_MODEL}" in out.err
    assert "Recovery attempted: no" in out.err
    assert "Model calls: 1" in out.err
    assert "Input tokens: 100" in out.err
    assert "Output tokens: 50" in out.err
    assert "incomplete" not in out.err


def test_cli_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _run_cli() == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err


# --- Bounded recovery: more than three otherwise-valid findings ---


def test_five_valid_candidates_trigger_exactly_one_selection_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES)),
        _selection_message([4, 1, 2]),
    )

    run = _run()

    assert len(fake.calls) == 2
    assert run.usage.model_calls == 2
    assert run.usage.recovery_attempted is True
    selection_call = fake.calls[1]
    assert selection_call["model"] == _MODEL
    assert selection_call["max_tokens"] == expert.SELECTION_MAX_OUTPUT_TOKENS == 256
    assert selection_call["system"] == SELECTION_SYSTEM_PROMPT
    user_content = selection_call["messages"][0]["content"]
    assert _PLAN in user_content
    assert _PERSPECTIVE in user_content
    labelled = json.loads(user_content.split("Candidate findings:\n", 1)[1])
    assert [c["index"] for c in labelled] == [0, 1, 2, 3, 4]
    assert [c["title"] for c in labelled] == [c.title for c in _CANDIDATES]


def test_selected_findings_keep_original_wording_in_selection_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES)),
        _selection_message([3, 0]),
    )

    report = _run().report

    assert report.findings == [_CANDIDATES[3], _CANDIDATES[0]]
    assert [f.model_dump() for f in report.findings] == [
        _CANDIDATES[3].model_dump(),
        _CANDIDATES[0].model_dump(),
    ]


def test_empty_selection_yields_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(monkeypatch, _message(_report_json(_CANDIDATES)), _selection_message([]))

    assert _run().report.findings == []


def test_usage_is_summed_across_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES), input_tokens=900, output_tokens=1200),
        _selection_message([0], input_tokens=1300, output_tokens=12),
    )

    usage = _run().usage

    assert usage.input_tokens == 2200
    assert usage.output_tokens == 1212
    assert usage.calls_missing_usage == 0


def test_cli_recovery_success_reports_calls_and_summed_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES), input_tokens=900, output_tokens=1200),
        _selection_message([2, 1], input_tokens=1300, output_tokens=12),
    )

    assert _run_cli() == 0
    out = capsys.readouterr()
    # stdout is only the final report JSON.
    assert json.loads(out.out) == {
        "findings": [_CANDIDATES[2].model_dump(), _CANDIDATES[1].model_dump()]
    }
    assert "Recovery attempted: yes" in out.err
    assert "Model calls: 2" in out.err
    assert "Input tokens: 2200" in out.err
    assert "Output tokens: 1212" in out.err


@pytest.mark.parametrize(
    "response_text",
    [
        pytest.param("not json", id="malformed-json"),
        pytest.param(
            _report_json([*_CANDIDATES[:4], _CANDIDATES[4].model_copy(update={"title": " "})]),
            id="one-invalid-finding",
        ),
        pytest.param(
            json.dumps(
                {"findings": [*[c.model_dump() for c in _CANDIDATES[:4]], {"title": "Only"}]}
            ),
            id="incomplete-finding",
        ),
        pytest.param(_report_json(_CANDIDATES, extra_field="x"), id="extra-top-level-field"),
    ],
)
def test_other_validation_failures_do_not_trigger_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response_text: str
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(monkeypatch, _message(response_text))

    with pytest.raises(expert.ExpertError, match="schema") as exc_info:
        _run()

    assert len(fake.calls) == 1
    usage = exc_info.value.usage
    assert usage is not None
    assert usage.recovery_attempted is False


def test_too_many_findings_without_normal_completion_do_not_trigger_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(
        monkeypatch, _message(_report_json(_CANDIDATES), stop_reason="stop_sequence")
    )

    with pytest.raises(expert.ExpertError, match="schema"):
        _run()
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "selection",
    [
        pytest.param(_selection_message([1, 1]), id="duplicate"),
        pytest.param(_selection_message([5]), id="out-of-range"),
        pytest.param(_selection_message([-1]), id="negative"),
        pytest.param(_selection_message([1.0]), id="float"),
        pytest.param(_selection_message(["1"]), id="string"),
        pytest.param(_selection_message([True]), id="bool"),
        pytest.param(_selection_message([0, 1, 2, 3]), id="too-many"),
        pytest.param(
            _message(json.dumps({"selected_indices": [0], "note": "x"})), id="extra-field"
        ),
        pytest.param(_message("not json"), id="malformed-json"),
        pytest.param(_message(None), id="no-text"),
        pytest.param(_message(None, stop_reason="refusal"), id="refusal"),
        pytest.param(_message('{"selected_indices": [', stop_reason="max_tokens"), id="truncated"),
    ],
)
def test_invalid_selection_fails_without_a_third_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: Message
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES), input_tokens=900, output_tokens=1200),
        selection,
    )

    with pytest.raises(expert.ExpertError, match="[Ss]election") as exc_info:
        _run()

    assert len(fake.calls) == 2
    usage = exc_info.value.usage
    assert usage is not None
    assert usage.recovery_attempted is True
    assert usage.model_calls == 2
    assert usage.calls_missing_usage == 0
    assert usage.input_tokens == 900 + 100


def test_selection_api_error_fails_without_a_third_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES), input_tokens=900, output_tokens=1200),
        _api_error(),
    )

    with pytest.raises(expert.ExpertError, match="Authentication error") as exc_info:
        _run()

    assert len(fake.calls) == 2
    usage = exc_info.value.usage
    assert usage is not None
    assert usage.model_calls == 2
    assert usage.calls_missing_usage == 1
    assert (usage.input_tokens, usage.output_tokens) == (900, 1200)


def test_cli_recovery_failure_marks_usage_incomplete_and_hides_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES), input_tokens=900, output_tokens=1200),
        _api_error(),
    )

    assert _run_cli() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "Recovery attempted: yes" in out.err
    assert "Model calls: 2" in out.err
    assert "Input tokens: 900 (incomplete: usage unavailable for 1 of 2 calls)" in out.err
    assert "Output tokens: 1200 (incomplete" in out.err
    assert "dummy-test-key" not in out.err
    for candidate in _CANDIDATES:
        assert candidate.observation not in out.err


def test_cli_invalid_selection_keeps_model_output_out_of_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    marker = "SECRET-SELECTION-MARKER"
    _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES)),
        _message(json.dumps({"selected_indices": [marker]})),
    )

    assert _run_cli() == 1
    err = capsys.readouterr().err
    assert marker not in err
    assert "input_value" not in err
    assert "Finding selection was invalid" in err
    for candidate in _CANDIDATES:
        assert candidate.observation not in err


def test_recovery_never_exceeds_two_calls_even_if_more_are_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake = _install_fake(
        monkeypatch,
        _message(_report_json(_CANDIDATES)),
        _selection_message([0, 1, 2, 3]),
        _selection_message([0]),  # would succeed if a third call were ever made
    )

    with pytest.raises(expert.ExpertError):
        _run()
    assert len(fake.calls) == 2
