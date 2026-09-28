import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, TextBlock, Usage

from blindspot import pipeline
from blindspot.expert import ExpertError, ExpertRun, FailureDiagnostics, UsageSummary
from blindspot.prompts import (
    EXPERT_SYSTEM_PROMPT,
    SELECTION_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
    TEAM_SELECTOR_SYSTEM_PROMPT,
)
from blindspot.schemas import (
    ExpertDefinition,
    ExpertReport,
    Finding,
    SynthesisReport,
    SynthesizedFinding,
    TeamPlan,
)
from blindspot.synthesis import (
    ExpertInput,
    SynthesisError,
    SynthesisRun,
    source_map,
    synthesize,
)
from blindspot.team import TeamSelection, TeamSelectionError

_PLAN = "Open a coffee shop in Warsaw."
_MODEL = "claude-haiku-4-5"

_TEAM = TeamPlan(
    experts=[
        ExpertDefinition(
            perspective="Financial",
            objective="First-year cash flow against rent and fit-out costs.",
            questions=["What funding covers the first months?"],
        ),
        ExpertDefinition(
            perspective=" Coffee Supply Chain ",  # custom, with stray whitespace
            objective="Reliability of bean and equipment supply.",
            questions=["Which suppliers are arranged?", "What happens if one fails?"],
        ),
        ExpertDefinition(
            perspective="Legal & Regulatory",
            objective="Permits needed before opening.",
            questions=["Which permits apply to food service here?"],
        ),
    ]
)

ExpertHandler = Callable[[str, str, str | None, list[str] | None], Awaitable[ExpertRun]]


def _usage(calls: int, input_tokens: int, output_tokens: int, **kwargs: Any) -> UsageSummary:
    return UsageSummary(
        model_calls=calls,
        calls_with_usage=kwargs.pop("calls_with_usage", calls),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        **kwargs,
    )


def _selection(team: TeamPlan = _TEAM) -> TeamSelection:
    return TeamSelection(team=team, model=f"{_MODEL}-20260101", usage=_usage(1, 400, 200))


def _finding(label: str) -> Finding:
    return Finding(
        title=f"{label} finding",
        observation=f"Observation from {label}.",
        why_it_matters="Why it matters.",
        assumptions=[],
        question_to_check="Question?",
    )


def _run_for(perspective: str, *, recovery: bool = False) -> ExpertRun:
    calls = 2 if recovery else 1
    return ExpertRun(
        report=ExpertReport(findings=[_finding(perspective)]),
        model=_MODEL,
        usage=_usage(calls, 100 * calls, 50 * calls, recovery_attempted=recovery),
    )


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)  # no real .env; runs/ lands in tmp_path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    yield
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(var, None)


class _ExpertRecorder:
    def __init__(self, handler: ExpertHandler | None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler

    async def __call__(
        self,
        plan: str,
        perspective: str,
        *,
        objective: str | None = None,
        questions: list[str] | None = None,
    ) -> ExpertRun:
        self.calls.append(
            {
                "plan": plan,
                "perspective": perspective,
                "objective": objective,
                "questions": questions,
            }
        )
        if self._handler is not None:
            return await self._handler(plan, perspective, objective, questions)
        return _run_for(perspective)


def _synthesis_run(experts: list[ExpertInput]) -> SynthesisRun:
    first_id = next(iter(source_map(experts)))
    finding = SynthesizedFinding(
        title="Synthesized",
        observation="Observation.",
        why_it_matters="Why.",
        assumptions=[],
        question_to_check="Question?",
        source_finding_ids=[first_id],
    )
    return SynthesisRun(
        report=SynthesisReport(findings=[finding]),
        model=f"{_MODEL}-20260101",
        usage=_usage(1, 300, 150),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selection: TeamSelection | BaseException | None = None,
    handler: ExpertHandler | None = None,
) -> _ExpertRecorder:
    outcome = _selection() if selection is None else selection

    async def fake_select_team(plan: str) -> TeamSelection:
        assert plan == _PLAN
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fake_synthesize(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
        return _synthesis_run(experts)

    recorder = _ExpertRecorder(handler)
    monkeypatch.setattr(pipeline, "select_team", fake_select_team)
    monkeypatch.setattr(pipeline, "run_expert", recorder)
    monkeypatch.setattr(pipeline, "synthesize", fake_synthesize)
    return recorder


def _run_dir(tmp_path: Path) -> Path:
    (directory,) = (tmp_path / "runs").iterdir()
    assert directory.name.startswith("team-")
    return directory


def _auto() -> pipeline.AutoTeamRun:
    return asyncio.run(pipeline.run_auto_team(_PLAN))


def _selection_error() -> TeamSelectionError:
    return TeamSelectionError(
        "Team selection output was invalid (experts: List should have at most 3 items)",
        _usage(1, 400, 900),
        returned_model=f"{_MODEL}-20260101",
        diagnostics=FailureDiagnostics(failure_stage="team_selection", stop_reason="end_turn"),
    )


# --- Selection gating ---


def test_invalid_selector_output_starts_no_experts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install(monkeypatch, selection=_selection_error())

    auto = _auto()

    assert recorder.calls == []
    assert auto.status == "failed"
    assert auto.result is None
    saved = json.loads((_run_dir(tmp_path) / "team.json").read_text())
    assert saved["team"] is None
    assert saved["plan"] == _PLAN
    assert saved["team_selection"]["status"] == "failed"
    assert saved["team_selection"]["model"] == f"{_MODEL}-20260101"
    assert saved["team_selection"]["diagnostics"]["failure_stage"] == "team_selection"
    assert saved["team_selection"]["usage"]["input_tokens"] == 400
    assert not (_run_dir(tmp_path) / "result.json").exists()


def test_blank_plan_and_missing_config_create_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install(monkeypatch)

    with pytest.raises(ValueError, match="plan"):
        asyncio.run(pipeline.run_auto_team("  "))
    monkeypatch.delenv("ANTHROPIC_MODEL")
    with pytest.raises(ValueError, match="ANTHROPIC_MODEL"):
        _auto()

    assert recorder.calls == []
    assert not (tmp_path / "runs").exists()


# --- Assignments and concurrency ---


def test_each_expert_receives_only_its_own_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install(monkeypatch)

    auto = _auto()

    assert auto.status == "success"
    by_perspective = {call["perspective"]: call for call in recorder.calls}
    assert set(by_perspective) == {"Financial", "Coffee Supply Chain", "Legal & Regulatory"}
    for expert in _TEAM.experts:
        call = by_perspective[expert.perspective.strip()]
        assert call["plan"] == _PLAN
        assert call["objective"] == expert.objective
        assert call["questions"] == expert.questions
    # Results keep team order, with each expert's assignment attached.
    assert [o.perspective for o in auto.result.outcomes] == [  # type: ignore[union-attr]
        "Financial",
        "Coffee Supply Chain",
        "Legal & Regulatory",
    ]


def test_assigned_experts_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    started = 0
    all_started = asyncio.Event()

    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        nonlocal started
        started += 1
        if started == 3:
            all_started.set()
        # Sequential execution would block here until the timeout.
        await asyncio.wait_for(all_started.wait(), timeout=5)
        return _run_for(perspective)

    _install(monkeypatch, handler=handler)

    assert _auto().status == "success"


def test_team_is_saved_before_any_expert_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        seen.append(json.loads((_run_dir(tmp_path) / "team.json").read_text()))
        return _run_for(perspective)

    _install(monkeypatch, handler=handler)

    _auto()

    assert len(seen) == 3
    assert all(s["team"] == _TEAM.model_dump() for s in seen)
    assert all(s["team_selection"]["status"] == "success" for s in seen)


def test_team_survives_an_interrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        raise RuntimeError("bug")

    _install(monkeypatch, handler=handler)

    with pytest.raises(ExceptionGroup):
        pipeline.main(["--plan", _PLAN, "--auto-team"])

    directory = _run_dir(tmp_path)
    assert json.loads((directory / "team.json").read_text())["team"] == _TEAM.model_dump()
    assert not (directory / "result.json").exists()
    assert f"saved files are in: {directory.relative_to(tmp_path)}" in capsys.readouterr().err


# --- Outcomes, usage, and saved results ---


def _mixed_handler() -> ExpertHandler:
    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        if perspective == "Financial":
            return _run_for(perspective, recovery=True)  # 2 calls: 200 in / 100 out
        if perspective == "Coffee Supply Chain":
            raise ExpertError(
                "Connection error: boom",
                _usage(2, 100, 50, calls_with_usage=1, recovery_attempted=True),
                returned_model=_MODEL,
                diagnostics=FailureDiagnostics(failure_stage="selection"),
            )
        return _run_for(perspective)  # 1 call: 100 in / 50 out

    return handler


def test_partial_failure_preserves_successful_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, handler=_mixed_handler())

    auto = _auto()

    assert auto.status == "partial"
    assert auto.result is not None
    financial, supply, legal = auto.result.outcomes
    assert financial.run is not None and financial.run.report.findings == [_finding("Financial")]
    assert supply.status == "failed" and supply.error == "Connection error: boom"
    assert legal.run is not None


def test_cli_auto_team_json_usage_and_saved_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, handler=_mixed_handler())

    assert pipeline.main(["--plan", _PLAN, "--auto-team"]) == 1

    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["status"] == "partial"
    assert data["team"] == _TEAM.model_dump()
    assert data["team_selection"] == {
        "status": "success",
        "model": f"{_MODEL}-20260101",
        "error": None,
        "diagnostics": None,
        "usage": {
            "model_calls": 1,
            "recovery_attempted": False,
            "input_tokens": 400,
            "output_tokens": 200,
            "usage_incomplete": False,
        },
    }
    assert [e["perspective"] for e in data["experts"]] == [
        "Financial",
        "Coffee Supply Chain",
        "Legal & Regulatory",
    ]
    assert data["experts"][1]["assignment"] == _TEAM.experts[1].model_dump()
    assert data["experts"][1]["diagnostics"]["failure_stage"] == "selection"
    assert data["synthesis"]["status"] == "completed"
    assert data["synthesis"]["usage"]["input_tokens"] == 300
    # Selector (1 call, 400/200) + experts (2 + 2 + 1 calls; 200+100+100 / 100+50+50)
    # + synthesis (1 call, 300/150). Each expert's usage already includes its
    # recovery, and synthesis is added once, so nothing is counted twice.
    assert data["total_usage"] == {
        "model_calls": 7,
        "input_tokens": 1100,
        "output_tokens": 550,
        "usage_incomplete": True,
    }

    # The team is printed to stderr before the experts' results.
    assert out.err.index("Selected team (3 experts):") < out.err.index("Status: partial")
    assert "- Financial: First-year cash flow against rent and fit-out costs." in out.err
    assert "    ? Which suppliers are arranged?" in out.err
    assert "Team selector: 1 call, input tokens 400, output tokens 200" in out.err
    assert "Synthesis: completed (1 findings), input tokens 300, output tokens 150" in out.err
    assert "Model calls: 7 (recoveries: 2)" in out.err
    assert "Input tokens: 1100 (incomplete: usage unavailable for 1 of 7 calls)" in out.err

    directory = _run_dir(tmp_path)
    saved = json.loads((directory / "result.json").read_text())
    assert saved["plan"] == _PLAN
    assert {k: saved[k] for k in data} == data
    assert f"Results saved in: {directory.relative_to(tmp_path)}" in out.err


def test_cli_selection_failure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _install(monkeypatch, selection=_selection_error())

    assert pipeline.main(["--plan", _PLAN, "--auto-team"]) == 1

    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["status"] == "failed"
    assert data["team"] is None
    assert data["experts"] == []
    assert data["total_usage"]["model_calls"] == 1
    assert "Error: Team selection output was invalid" in out.err
    assert "Selected team" not in out.err
    assert recorder.calls == []


# --- CLI modes ---


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--plan", _PLAN, "--auto-team", "--perspective", "Financial"], id="both"),
        pytest.param(["--plan", _PLAN], id="neither"),
    ],
)
def test_auto_team_and_perspective_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    recorder = _install(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        pipeline.main(argv)

    assert exc_info.value.code == 2
    assert recorder.calls == []


def test_manual_mode_output_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _install(monkeypatch)

    assert pipeline.main(["--plan", _PLAN, "--perspective", "Financial"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"status", "experts"}
    assert "assignment" not in data["experts"][0]
    assert recorder.calls == [
        {"plan": _PLAN, "perspective": "Financial", "objective": None, "questions": None}
    ]
    assert not (tmp_path / "runs").exists()


# --- End to end with a fake Anthropic client (real selector and experts) ---


class _FakeMessages:
    """Selector returns `team`; experts return one finding, except the expert whose
    perspective is `over_limit` returns four, which triggers its recovery call."""

    def __init__(self, team: TeamPlan, over_limit: str | None = None) -> None:
        self._team = team
        self._over_limit = over_limit
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        content = kwargs["messages"][0]["content"]
        if kwargs["system"] == TEAM_SELECTOR_SYSTEM_PROMPT:
            text = self._team.model_dump_json()
        elif kwargs["system"] == SELECTION_SYSTEM_PROMPT:
            text = json.dumps({"selected_indices": [3, 1]})
        elif kwargs["system"] == SYNTHESIS_SYSTEM_PROMPT:
            finding = {
                "title": "Synthesized",
                "observation": "Observation.",
                "why_it_matters": "Why.",
                "assumptions": [],
                "question_to_check": "Question?",
                "source_finding_ids": ["e1-f1"],
            }
            text = json.dumps({"findings": [finding]})
        elif self._over_limit and f"Perspective:\n{self._over_limit}\n" in content:
            text = json.dumps({"findings": [_finding(f"c{i}").model_dump() for i in range(4)]})
        else:
            text = json.dumps({"findings": [_finding("any").model_dump()]})
        return Message(
            id="msg_test",
            type="message",
            role="assistant",
            model=f"{kwargs['model']}-20260101",
            content=[TextBlock(type="text", text=text)],
            stop_reason="end_turn",
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


def test_end_to_end_experts_see_only_their_own_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _FakeMessages(_TEAM)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))

    auto = _auto()

    assert auto.status == "success"
    selector_call, *expert_calls = messages.calls
    assert selector_call["system"] == TEAM_SELECTOR_SYSTEM_PROMPT
    assert selector_call["max_tokens"] == 1500
    *expert_calls, synthesis_call = expert_calls
    assert synthesis_call["system"] == SYNTHESIS_SYSTEM_PROMPT
    assert len(expert_calls) == 3
    contents = [c["messages"][0]["content"] for c in expert_calls]
    for expert in _TEAM.experts:
        (own,) = [c for c in contents if f"Perspective:\n{expert.perspective.strip()}\n" in c]
        assert own.startswith(f"Plan:\n{_PLAN}\n\n")
        assert f"Objective:\n{expert.objective}" in own
        for other in _TEAM.experts:
            if other is not expert:
                assert other.objective not in own
                assert other.perspective.strip() not in own
    assert all(c["system"] == EXPERT_SYSTEM_PROMPT for c in expert_calls)
    total = pipeline.auto_result_json(auto)["total_usage"]
    assert total["model_calls"] == 5  # 1 selector + 3 experts (no recovery) + 1 synthesis
    assert total["input_tokens"] == 50  # 10 per call


def test_end_to_end_recovery_keeps_the_experts_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: an assigned expert's recovery call must see the same
    # objective and questions as its initial call, and nothing from other experts.
    supply = _TEAM.experts[1]
    messages = _FakeMessages(_TEAM, over_limit=supply.perspective.strip())
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))

    auto = _auto()

    assert auto.status == "success"
    selection_calls = [c for c in messages.calls if c["system"] == SELECTION_SYSTEM_PROMPT]
    (selection_call,) = selection_calls
    (initial_call,) = [
        c
        for c in messages.calls
        if c["system"] == EXPERT_SYSTEM_PROMPT
        and f"Perspective:\n{supply.perspective.strip()}\n" in c["messages"][0]["content"]
    ]
    initial = initial_call["messages"][0]["content"]
    selection = selection_call["messages"][0]["content"]
    assert selection.startswith(initial + "\n\nCandidate findings:\n")
    assert f"Objective:\n{supply.objective}" in selection
    assert "1. Which suppliers are arranged?\n2. What happens if one fails?" in selection
    for other in (_TEAM.experts[0], _TEAM.experts[2]):
        assert other.objective not in selection

    assert auto.result is not None
    recovered = auto.result.outcomes[1]
    assert recovered.run is not None and recovered.run.usage.recovery_attempted
    assert [f.title for f in recovered.run.report.findings] == ["c3 finding", "c1 finding"]
    # 1 selector + 3 experts + 1 recovery + 1 synthesis
    assert pipeline.auto_result_json(auto)["total_usage"]["model_calls"] == 6


# --- Synthesis stage ---

SynthesisFake = Callable[[str, list[ExpertInput]], Awaitable[SynthesisRun]]


def _install_synthesis(monkeypatch: pytest.MonkeyPatch, fake: SynthesisFake) -> list[Any]:
    calls: list[Any] = []

    async def recording(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
        calls.append((plan, experts))
        return await fake(plan, experts)

    monkeypatch.setattr(pipeline, "synthesize", recording)
    return calls


async def _never_called(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
    raise AssertionError("synthesis must not be called")


def _synthesis_error() -> SynthesisError:
    return SynthesisError(
        "Synthesis cited 1 unknown source finding ID(s)",
        _usage(1, 700, 350),
        returned_model=f"{_MODEL}-20260101",
        diagnostics=FailureDiagnostics(failure_stage="synthesis", stop_reason="end_turn"),
    )


def _cli_auto(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any], str]:
    code = pipeline.main(["--plan", _PLAN, "--auto-team"])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.err


def test_synthesis_receives_successful_findings_and_missing_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, handler=_mixed_handler())
    calls = _install_synthesis(monkeypatch, lambda p, e: _async(_synthesis_run(e)))

    _auto()

    ((plan, experts),) = calls
    assert plan == _PLAN
    assert [(e.position, e.assignment, e.report is not None) for e in experts] == [
        (1, _TEAM.experts[0], True),
        (2, _TEAM.experts[1], False),  # failed: unavailable, no report
        (3, _TEAM.experts[2], True),
    ]


def test_synthesis_input_marks_failed_perspective_without_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SECRET-ERROR-MARKER"

    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        if perspective == "Coffee Supply Chain":
            raise ExpertError(f"Connection error: {marker}", _usage(1, 0, 0, calls_with_usage=0))
        return _run_for(perspective)

    _install(monkeypatch, handler=handler)
    # Real synthesize with a fake SDK client (selector and experts stay mocked).
    captured: list[str] = []

    class _Messages:
        async def create(self, **kwargs: Any) -> Message:
            captured.append(kwargs["messages"][0]["content"])
            text = json.dumps({"findings": []})
            return Message(
                id="m",
                type="message",
                role="assistant",
                model=_MODEL,
                content=[TextBlock(type="text", text=text)],
                stop_reason="end_turn",
                stop_sequence=None,
                usage=Usage(input_tokens=1, output_tokens=1),
            )

    messages = _Messages()
    monkeypatch.setattr(pipeline, "synthesize", synthesize)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(messages))  # type: ignore[arg-type]

    auto = _auto()

    (content,) = captured
    assert marker not in content
    unavailable = json.loads(
        content.split("missing coverage, not evidence that no concerns exist):\n")[1]
    )
    assert unavailable == [
        {
            "expert": "e2",
            "perspective": "Coffee Supply Chain",
            "objective": _TEAM.experts[1].objective,
            "questions": _TEAM.experts[1].questions,
            "status": "unavailable",
        }
    ]
    assert '"id": "e1-f1"' in content and '"id": "e3-f1"' in content
    assert auto.synthesis is not None and auto.synthesis.status == "completed"


def test_partial_expert_failure_stays_visible_when_synthesis_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, handler=_mixed_handler())

    code, data, err = _cli_auto(capsys)

    assert code == 1
    assert data["status"] == "partial"
    assert data["synthesis"]["status"] == "completed"
    assert [e["status"] for e in data["experts"]] == ["success", "failed", "success"]
    assert data["experts"][1]["error"] == "Connection error: boom"
    assert "Status: partial (2/3 experts succeeded)" in err


def test_full_success_with_synthesis_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch)

    code, data, _ = _cli_auto(capsys)

    assert code == 0
    assert data["status"] == "success"
    synthesis_json = data["synthesis"]
    assert synthesis_json["status"] == "completed"
    assert synthesis_json["reason"] is None
    assert synthesis_json["model"] == f"{_MODEL}-20260101"
    assert synthesis_json["error"] is None and synthesis_json["diagnostics"] is None
    assert synthesis_json["elapsed_seconds"] >= 0
    assert synthesis_json["report"]["findings"][0]["source_finding_ids"] == ["e1-f1"]
    # Every cited ID resolves, through the saved mapping, to an unchanged original finding.
    for source_id, ref in data["sources"].items():
        original = data["experts"][ref["expert_position"] - 1]["report"]["findings"]
        assert original[ref["finding_position"] - 1]["title"] == ref["title"]
        assert source_id == f"e{ref['expert_position']}-f{ref['finding_position']}"
    assert list(data["sources"]) == ["e1-f1", "e2-f1", "e3-f1"]
    # Selector 1 + experts 3 + synthesis 1, each counted once.
    assert data["total_usage"] == {
        "model_calls": 5,
        "input_tokens": 400 + 300 + 300,
        "output_tokens": 200 + 150 + 150,
        "usage_incomplete": False,
    }


def test_empty_successful_reports_skip_the_synthesis_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        return ExpertRun(report=ExpertReport(findings=[]), model=_MODEL, usage=_usage(1, 100, 10))

    _install(monkeypatch, handler=handler)
    calls = _install_synthesis(monkeypatch, _never_called)

    code, data, err = _cli_auto(capsys)

    assert calls == []
    assert code == 0
    assert data["synthesis"]["status"] == "no_findings"
    assert data["synthesis"]["report"] == {"findings": []}
    assert data["synthesis"]["usage"] is None
    assert data["sources"] == {}
    assert data["total_usage"]["model_calls"] == 4
    assert "Synthesis: no_findings (0 findings)" in err


def test_all_expert_failure_skips_synthesis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def handler(
        plan: str, perspective: str, objective: str | None, questions: list[str] | None
    ) -> ExpertRun:
        raise ExpertError("Connection error: boom", _usage(1, 0, 0, calls_with_usage=0))

    _install(monkeypatch, handler=handler)
    calls = _install_synthesis(monkeypatch, _never_called)

    code, data, err = _cli_auto(capsys)

    assert calls == []
    assert code == 1
    assert data["status"] == "failed"
    assert data["synthesis"]["status"] == "unavailable"
    assert data["synthesis"]["reason"] == "all experts failed"
    assert data["synthesis"]["report"] is None
    assert "Synthesis: unavailable (all experts failed)" in err


def test_selection_failure_reports_synthesis_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, selection=_selection_error())
    calls = _install_synthesis(monkeypatch, _never_called)

    code, data, _ = _cli_auto(capsys)

    assert calls == []
    assert code == 1
    assert data["synthesis"]["status"] == "unavailable"
    assert data["synthesis"]["reason"] == "team selection failed"


def test_synthesis_failure_keeps_expert_reports_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch)

    async def failing(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
        raise _synthesis_error()

    _install_synthesis(monkeypatch, failing)

    code, data, err = _cli_auto(capsys)

    assert code == 1
    assert data["status"] == "success"  # experts all succeeded
    assert data["synthesis"]["status"] == "failed"
    assert data["synthesis"]["report"] is None
    assert data["synthesis"]["model"] == f"{_MODEL}-20260101"
    assert data["synthesis"]["error"] == "Synthesis cited 1 unknown source finding ID(s)"
    assert data["synthesis"]["diagnostics"]["failure_stage"] == "synthesis"
    assert [e["report"]["findings"][0]["title"] for e in data["experts"]] == [
        "Financial finding",
        "Coffee Supply Chain finding",
        "Legal & Regulatory finding",
    ]
    assert data["total_usage"]["input_tokens"] == 400 + 300 + 700
    assert "Error: Synthesis cited 1 unknown source finding ID(s)" in err
    saved = json.loads((_run_dir(tmp_path) / "result.json").read_text())
    assert saved["synthesis"]["status"] == "failed"
    assert saved["experts"] == data["experts"]


def test_expert_stage_is_saved_before_synthesis_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, handler=_mixed_handler())
    seen: list[dict[str, Any]] = []

    async def checking(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
        directory = _run_dir(tmp_path)
        assert (directory / "team.json").exists()
        assert not (directory / "result.json").exists()
        seen.append(json.loads((directory / "experts.json").read_text()))
        return _synthesis_run(experts)

    _install_synthesis(monkeypatch, checking)

    _auto()

    (stage,) = seen
    assert stage["plan"] == _PLAN
    assert "synthesis" not in stage
    assert [e["status"] for e in stage["experts"]] == ["success", "failed", "success"]
    assert stage["sources"] == {
        "e1-f1": {
            "expert_position": 1,
            "perspective": "Financial",
            "finding_position": 1,
            "title": "Financial finding",
        },
        "e3-f1": {
            "expert_position": 3,
            "perspective": "Legal & Regulatory",
            "finding_position": 1,
            "title": "Legal & Regulatory finding",
        },
    }


def test_expert_stage_survives_interrupted_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)

    async def interrupted(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
        raise asyncio.CancelledError

    _install_synthesis(monkeypatch, interrupted)

    with pytest.raises(asyncio.CancelledError):
        pipeline.main(["--plan", _PLAN, "--auto-team"])

    directory = _run_dir(tmp_path)
    stage = json.loads((directory / "experts.json").read_text())
    assert len(stage["experts"]) == 3
    assert not (directory / "result.json").exists()


def test_manual_mode_never_synthesizes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch)
    calls = _install_synthesis(monkeypatch, _never_called)

    assert pipeline.main(["--plan", _PLAN, "--perspective", "Financial"]) == 0

    assert calls == []
    assert "synthesis" not in json.loads(capsys.readouterr().out)


async def _async(value: SynthesisRun) -> SynthesisRun:
    return value
