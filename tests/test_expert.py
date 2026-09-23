import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import httpx2
import pydantic
import pytest

from blindspot import expert
from blindspot.schemas import ExpertReport, Finding

_VALID_FINDING = Finding(
    title="No cash-flow buffer",
    observation="The plan doesn't mention a reserve for slow months.",
    why_it_matters="A single bad month could force the shop to close early.",
    assumptions=["Revenue varies significantly by season"],
    question_to_check="How many months of expenses could the business cover with no revenue?",
)


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
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeParsedResponse:
    stop_reason: str
    parsed_output: ExpertReport | None
    model: str = "claude-haiku-4-5"
    usage: _FakeUsage = field(
        default_factory=lambda: _FakeUsage(input_tokens=100, output_tokens=50)
    )


class _FakeMessages:
    def __init__(self, response: _FakeParsedResponse | None, error: BaseException | None) -> None:
        self._response = response
        self._error = error

    async def parse(self, **_: object) -> _FakeParsedResponse:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class _FakeAsyncAnthropic:
    def __init__(self, response: _FakeParsedResponse | None, error: BaseException | None) -> None:
        self.messages = _FakeMessages(response=response, error=error)

    async def __aenter__(self) -> "_FakeAsyncAnthropic":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _make_fake_client(
    response: _FakeParsedResponse | None = None, error: BaseException | None = None
) -> Callable[..., _FakeAsyncAnthropic]:
    def factory(**_: object) -> _FakeAsyncAnthropic:
        return _FakeAsyncAnthropic(response=response, error=error)

    return factory


def test_run_expert_rejects_blank_plan() -> None:
    with pytest.raises(ValueError, match="plan"):
        asyncio.run(expert.run_expert("   ", "Financial"))


def test_run_expert_rejects_blank_perspective() -> None:
    with pytest.raises(ValueError, match="perspective"):
        asyncio.run(expert.run_expert("Open a coffee shop.", "  "))


def test_run_expert_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake_response = _FakeParsedResponse(
        stop_reason="end_turn",
        parsed_output=ExpertReport(findings=[_VALID_FINDING]),
    )
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(response=fake_response))

    run = asyncio.run(expert.run_expert("Open a coffee shop in Warsaw.", "Financial"))

    assert run.report.findings == [_VALID_FINDING]
    assert run.model == "claude-haiku-4-5"
    assert run.input_tokens == 100
    assert run.output_tokens == 50


def test_run_expert_success_zero_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake_response = _FakeParsedResponse(
        stop_reason="end_turn",
        parsed_output=ExpertReport(findings=[]),
    )
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(response=fake_response))

    run = asyncio.run(expert.run_expert("Open a coffee shop in Warsaw.", "Financial"))

    assert run.report.findings == []


def test_run_expert_refusal_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake_response = _FakeParsedResponse(stop_reason="refusal", parsed_output=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(response=fake_response))

    with pytest.raises(expert.ExpertError, match="declined"):
        asyncio.run(expert.run_expert("Open a coffee shop in Warsaw.", "Financial"))


def test_run_expert_truncation_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake_response = _FakeParsedResponse(stop_reason="max_tokens", parsed_output=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(response=fake_response))

    with pytest.raises(expert.ExpertError, match="truncated"):
        asyncio.run(expert.run_expert("Open a coffee shop in Warsaw.", "Financial"))


def test_run_expert_invalid_output_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    try:
        ExpertReport.model_validate({"findings": "not-a-list"})
        raise AssertionError("expected a ValidationError")
    except pydantic.ValidationError as real_validation_error:
        validation_error: pydantic.ValidationError = real_validation_error

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(error=validation_error))

    with pytest.raises(expert.ExpertError, match="schema"):
        asyncio.run(expert.run_expert("Open a coffee shop in Warsaw.", "Financial"))


def test_cli_invalid_output_keeps_raw_model_content_out_of_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    marker = "SECRET-MARKER-XYZ"
    try:
        # Simulates malformed model output that includes an unexpected field
        # carrying arbitrary (here, sensitive-looking) model-generated text.
        ExpertReport.model_validate({"findings": [], "leaked_field": marker})
        raise AssertionError("expected a ValidationError")
    except pydantic.ValidationError as real_validation_error:
        validation_error: pydantic.ValidationError = real_validation_error

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(error=validation_error))

    exit_code = expert.main(
        ["--plan", "Open a coffee shop in Warsaw.", "--perspective", "Financial"]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert marker not in err
    assert "input_value" not in err
    assert "leaked_field" in err
    assert "Extra inputs are not permitted" in err


def test_run_expert_api_failure_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        401,
        request=request,
        json={
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        },
    )
    error = anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(error=error))

    with pytest.raises(expert.ExpertError, match="Authentication error") as exc_info:
        asyncio.run(expert.run_expert("Open a coffee shop in Warsaw.", "Financial"))

    assert "dummy-test-key" not in str(exc_info.value)


def test_cli_success_prints_json_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake_response = _FakeParsedResponse(
        stop_reason="end_turn",
        parsed_output=ExpertReport(findings=[_VALID_FINDING]),
    )
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(response=fake_response))

    exit_code = expert.main(
        ["--plan", "Open a coffee shop in Warsaw.", "--perspective", "Financial"]
    )

    assert exit_code == 0
    out = capsys.readouterr()
    assert '"title": "No cash-flow buffer"' in out.out
    assert "Model: claude-haiku-4-5" in out.err
    assert "Input tokens: 100" in out.err
    assert "Output tokens: 50" in out.err


def test_cli_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = expert.main(
        ["--plan", "Open a coffee shop in Warsaw.", "--perspective", "Financial"]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
