import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import anthropic
import httpx2
import pytest

from blindspot import llm_smoke


@pytest.fixture(autouse=True)
def _clean_env() -> Iterator[None]:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


def _setup_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, model: str | None = "claude-opus-4-8"
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    if model is not None:
        monkeypatch.setenv("ANTHROPIC_MODEL", model)


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeMessage:
    content: list[_FakeTextBlock]
    model: str
    usage: _FakeUsage


class _FakeMessages:
    def __init__(self, response: _FakeMessage | None, error: BaseException | None) -> None:
        self._response = response
        self._error = error

    async def create(self, **_: object) -> _FakeMessage:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class _FakeAsyncAnthropic:
    def __init__(self, response: _FakeMessage | None, error: BaseException | None) -> None:
        self.messages = _FakeMessages(response=response, error=error)

    async def __aenter__(self) -> "_FakeAsyncAnthropic":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _make_fake_client(
    response: _FakeMessage | None = None, error: BaseException | None = None
) -> Callable[..., _FakeAsyncAnthropic]:
    def factory(**_: object) -> _FakeAsyncAnthropic:
        return _FakeAsyncAnthropic(response=response, error=error)

    return factory


def test_successful_response_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    fake_message = _FakeMessage(
        content=[_FakeTextBlock(text="Blindspot connection OK.")],
        model="claude-opus-4-8",
        usage=_FakeUsage(input_tokens=12, output_tokens=5),
    )
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _make_fake_client(response=fake_message))

    exit_code = llm_smoke.main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Blindspot connection OK." in out
    assert "claude-opus-4-8" in out
    assert "12" in out
    assert "5" in out


def test_missing_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = llm_smoke.main()

    assert exit_code == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_missing_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_env(tmp_path, monkeypatch, model=None)

    exit_code = llm_smoke.main()

    assert exit_code == 1
    assert "ANTHROPIC_MODEL" in capsys.readouterr().err


def test_api_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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

    exit_code = llm_smoke.main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Authentication error" in err
    assert "dummy-test-key" not in err
