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

from blindspot import team
from blindspot.prompts import TEAM_SELECTOR_SYSTEM_PROMPT
from blindspot.schemas import ExpertDefinition, TeamPlan

_PLAN = "Open a coffee shop in Warsaw."
_MODEL = "claude-haiku-4-5"


def _expert(perspective: str = "Financial", **overrides: object) -> dict[str, object]:
    return {
        "perspective": perspective,
        "objective": f"Examine {perspective.lower()} aspects of the plan.",
        "questions": [f"What {perspective.lower()} arrangements exist?"],
        **overrides,
    }


# --- Team schema ---


def test_valid_team() -> None:
    plan = TeamPlan.model_validate(
        {"experts": [_expert("Financial"), _expert("Legal & Regulatory")]}
    )
    assert [e.perspective for e in plan.experts] == ["Financial", "Legal & Regulatory"]


def test_custom_perspective_is_accepted() -> None:
    plan = TeamPlan.model_validate({"experts": [_expert("Coffee Supply Chain")]})
    assert plan.experts[0].perspective == "Coffee Supply Chain"


def test_expert_allows_up_to_three_questions() -> None:
    expert = ExpertDefinition.model_validate(_expert(questions=["A?", "B?", "C?"]))
    assert expert.questions == ["A?", "B?", "C?"]


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"experts": []}, id="no-experts"),
        pytest.param({"experts": [_expert(p) for p in "ABCD"]}, id="four-experts"),
        pytest.param({"experts": [_expert(questions=[])]}, id="no-questions"),
        pytest.param({"experts": [_expert(questions=["a", "b", "c", "d"])]}, id="four-questions"),
        pytest.param({"experts": [_expert(questions=["ok?", "  "])]}, id="blank-question"),
        pytest.param({"experts": [_expert(perspective="  ")]}, id="blank-perspective"),
        pytest.param({"experts": [_expert(objective="")]}, id="blank-objective"),
        pytest.param({"experts": [_expert(skills=["x"])]}, id="extra-expert-field"),
        pytest.param({"experts": [_expert()], "rationale": "x"}, id="extra-team-field"),
        pytest.param(
            {"experts": [{"perspective": "Financial", "questions": ["Q?"]}]}, id="missing-field"
        ),
        pytest.param(
            {"experts": [_expert("Financial"), _expert(" financial ")]},
            id="duplicate-perspective",
        ),
    ],
)
def test_invalid_team_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(pydantic.ValidationError):
        TeamPlan.model_validate(data)


def test_duplicate_perspective_error_names_the_rule() -> None:
    with pytest.raises(pydantic.ValidationError, match="unique"):
        TeamPlan.model_validate({"experts": [_expert("Technical"), _expert("TECHNICAL")]})


def test_catalogue_names() -> None:
    assert team.PERSPECTIVE_CATALOGUE == (
        "Financial",
        "Legal & Regulatory",
        "Operations & Logistics",
        "Timeline & Dependencies",
        "Human & Behavioral",
        "Health & Safety",
        "Security & Privacy",
        "Technical",
        "Social & Relationships",
        "Environmental",
        "Travel & Mobility",
    )


# --- Selector call (fake Anthropic client) ---


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


def _message(
    text: str | None, stop_reason: StopReason = "end_turn", input_tokens: int = 400
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
        usage=Usage(input_tokens=input_tokens, output_tokens=120),
    )


def _api_error() -> anthropic.AuthenticationError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(
        401,
        request=request,
        json={"type": "error", "error": {"type": "authentication_error", "message": "bad"}},
    )
    return anthropic.AuthenticationError("invalid x-api-key", response=response, body=None)


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
    clients: list[dict[str, object]] = []

    def factory(**kwargs: object) -> _FakeClient:
        clients.append(kwargs)
        return _FakeClient(messages)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
    messages.client_kwargs = clients  # type: ignore[attr-defined]
    return messages


def _select() -> team.TeamSelection:
    return asyncio.run(team.select_team(_PLAN))


def test_select_team_makes_one_structured_call(monkeypatch: pytest.MonkeyPatch) -> None:
    team_json = json.dumps({"experts": [_expert("Financial"), _expert("Coffee Supply Chain")]})
    fake = _install(monkeypatch, _message(team_json))

    selection = _select()

    assert [e.perspective for e in selection.team.experts] == ["Financial", "Coffee Supply Chain"]
    assert selection.model == f"{_MODEL}-20260101"
    assert (selection.usage.model_calls, selection.usage.input_tokens) == (1, 400)
    (call,) = fake.calls
    assert call["model"] == _MODEL
    assert call["max_tokens"] == 1500
    assert call["system"] == TEAM_SELECTOR_SYSTEM_PROMPT
    assert call["output_config"]["format"]["type"] == "json_schema"
    content = call["messages"][0]["content"]
    # The catalogue is application-supplied context, separate from the system prompt.
    catalogue = "\n".join(f"- {name}" for name in team.PERSPECTIVE_CATALOGUE)
    assert f"Perspective catalogue:\n{catalogue}" in content
    assert "Travel & Mobility" not in TEAM_SELECTOR_SYSTEM_PROMPT
    assert content.endswith(f"Plan:\n{_PLAN}")
    (client_kwargs,) = fake.client_kwargs  # type: ignore[attr-defined]
    assert client_kwargs["timeout"] == 60.0
    assert client_kwargs["max_retries"] == 0


@pytest.mark.parametrize(
    ("response", "stop_reason", "match"),
    [
        pytest.param(_message(None, stop_reason="refusal"), "refusal", "declined", id="refusal"),
        pytest.param(
            _message('{"experts": [', stop_reason="max_tokens"),
            "max_tokens",
            "truncated",
            id="trunc",
        ),
        pytest.param(_message(None), "end_turn", "no output", id="no-text"),
    ],
)
def test_selector_stop_failures(
    monkeypatch: pytest.MonkeyPatch, response: Message, stop_reason: str, match: str
) -> None:
    fake = _install(monkeypatch, response)

    with pytest.raises(team.TeamSelectionError, match=match) as exc_info:
        _select()

    assert len(fake.calls) == 1
    error = exc_info.value
    assert error.returned_model == f"{_MODEL}-20260101"
    assert error.usage is not None and error.usage.input_tokens == 400
    assert error.diagnostics is not None
    assert error.diagnostics.failure_stage == "team_selection"
    assert error.diagnostics.stop_reason == stop_reason
    assert error.diagnostics.recovery_skip_reason is None


@pytest.mark.parametrize(
    ("team_json", "expected"),
    [
        pytest.param(
            json.dumps({"experts": [_expert(p) for p in "ABCD"]}),
            {("team_plan", "experts", "too_long")},
            id="four-experts",
        ),
        pytest.param(
            json.dumps({"experts": [_expert("Financial"), _expert("financial")]}),
            {("team_plan", "<root>", "value_error")},
            id="duplicates",
        ),
        pytest.param("not json", {("team_plan", "<root>", "json_invalid")}, id="malformed"),
    ],
)
def test_invalid_selector_output_is_a_diagnosed_failure(
    monkeypatch: pytest.MonkeyPatch, team_json: str, expected: set[tuple[str, str, str]]
) -> None:
    fake = _install(monkeypatch, _message(team_json))

    with pytest.raises(team.TeamSelectionError, match="invalid") as exc_info:
        _select()

    assert len(fake.calls) == 1  # no retry, no repair
    diagnostics = exc_info.value.diagnostics
    assert diagnostics is not None
    assert diagnostics.failure_stage == "team_selection"
    assert diagnostics.stop_reason == "end_turn"
    assert diagnostics.validation_errors is not None
    assert {(i.schema, i.loc, i.type) for i in diagnostics.validation_errors} == expected


def test_selector_api_error_records_null_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _api_error())

    with pytest.raises(team.TeamSelectionError, match="Authentication error") as exc_info:
        _select()

    error = exc_info.value
    assert error.returned_model is None
    assert error.usage is not None and error.usage.calls_missing_usage == 1
    assert error.diagnostics is not None
    assert error.diagnostics.to_json() == {
        "failure_stage": "team_selection",
        "stop_reason": None,
        "recovery_skip_reason": None,
        "validation_errors": None,
    }
    assert "dummy-test-key" not in str(error)


def test_selector_errors_do_not_leak_model_generated_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "EVIL-SELECTOR-MARKER"
    team_json = json.dumps({"experts": [_expert(**{marker: marker})], marker: marker})
    _install(monkeypatch, _message(team_json))

    with pytest.raises(team.TeamSelectionError) as exc_info:
        _select()

    diagnostics = exc_info.value.diagnostics
    assert diagnostics is not None
    serialized = str(exc_info.value) + json.dumps(diagnostics.to_json())
    assert marker not in serialized
    assert "<unknown field>" in serialized


def test_blank_plan_is_rejected_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)

    with pytest.raises(ValueError, match="plan"):
        asyncio.run(team.select_team("  "))
    assert fake.calls == []
