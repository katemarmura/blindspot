"""Blindspot's multi-expert runner: explicit perspectives or an auto-selected team.

Run with:
    uv run python -m blindspot.pipeline --plan "..." \\
        --perspective "Financial" --perspective "Operations"
    uv run python -m blindspot.pipeline --plan "..." --auto-team

With `--perspective`, the caller supplies one to three perspectives. With
`--auto-team`, one selector call (`blindspot.team.select_team`) proposes one to
three expert assignments first; if it fails, no expert starts. Either way each
expert runs the existing single-expert call (`run_expert`) independently and
concurrently, seeing only the original plan and its own perspective (plus, for
an auto-selected team, its own objective and questions). Outcomes are returned
per expert, in input order, and there are no pipeline-level retries.

Only `--auto-team` adds a synthesis stage after the experts
(`blindspot.synthesis`): one call that turns successful experts' findings
into up to five cited considerations. The original expert reports are kept
unchanged alongside it. Manual runs never synthesize.
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.expert import (
    ExpertError,
    ExpertRun,
    FailureDiagnostics,
    UsageSummary,
    run_expert,
)
from blindspot.prompts import SYNTHESIS_SYSTEM_PROMPT, TEAM_SELECTOR_SYSTEM_PROMPT
from blindspot.runs import create_run_dir, git_state, utc_now, write_json
from blindspot.schemas import ExpertDefinition, SynthesisReport, TeamPlan
from blindspot.synthesis import (
    SYNTHESIS_MAX_OUTPUT_TOKENS,
    ExpertInput,
    SourceRef,
    SynthesisError,
    SynthesisRun,
    source_map,
    synthesize,
)
from blindspot.team import (
    PERSPECTIVE_CATALOGUE,
    TEAM_SELECTOR_MAX_OUTPUT_TOKENS,
    TeamSelection,
    TeamSelectionError,
    select_team,
)

MAX_PERSPECTIVES = 3

ExpertStatus = Literal["success", "failed"]
PipelineStatus = Literal["success", "partial", "failed"]


@dataclass(frozen=True)
class ExpertOutcome:
    """One expert's result: a successful `run` or a sanitized `error`, never both."""

    perspective: str
    elapsed_seconds: float
    run: ExpertRun | None = None
    error: str | None = None
    # On failure, whatever usage the expert recorded before failing (None if
    # it failed before making a call). On success, `run.usage`.
    failed_usage: UsageSummary | None = None
    # On failure, the model ID from a received response (None if no response
    # arrived). On success, `run.model`.
    failed_returned_model: str | None = None
    # On failure, structured diagnostics from the ExpertError; None on success.
    failed_diagnostics: FailureDiagnostics | None = None
    # The expert's assignment when it came from an auto-selected team.
    assignment: ExpertDefinition | None = None

    @property
    def status(self) -> ExpertStatus:
        return "success" if self.run is not None else "failed"

    @property
    def returned_model(self) -> str | None:
        return self.run.model if self.run is not None else self.failed_returned_model

    @property
    def usage(self) -> UsageSummary | None:
        return self.run.usage if self.run is not None else self.failed_usage


@dataclass(frozen=True)
class PipelineResult:
    outcomes: list[ExpertOutcome]
    elapsed_seconds: float

    @property
    def status(self) -> PipelineStatus:
        succeeded = sum(o.status == "success" for o in self.outcomes)
        if succeeded == len(self.outcomes):
            return "success"
        return "partial" if succeeded else "failed"


def _normalize_perspectives(perspectives: list[str]) -> list[str]:
    if not 1 <= len(perspectives) <= MAX_PERSPECTIVES:
        raise ValueError(
            f"between 1 and {MAX_PERSPECTIVES} perspectives are required (got {len(perspectives)})"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for perspective in perspectives:
        name = perspective.strip()
        if not name:
            raise ValueError("perspectives must not be blank")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"duplicate perspective: {name!r}")
        seen.add(key)
        normalized.append(name)
    return normalized


async def _run_one(
    plan: str, perspective: str, assignment: ExpertDefinition | None = None
) -> ExpertOutcome:
    start = time.monotonic()
    try:
        if assignment is None:
            run = await run_expert(plan, perspective)
        else:
            # Only this expert's own assignment; never other assignments or reports.
            run = await run_expert(
                plan,
                perspective,
                objective=assignment.objective,
                questions=assignment.questions,
            )
    except ExpertError as exc:
        # Only the expected, already-sanitized failure is captured. Anything
        # else (a bug, or cancellation) propagates to the TaskGroup.
        return ExpertOutcome(
            perspective=perspective,
            elapsed_seconds=time.monotonic() - start,
            error=str(exc),
            failed_usage=exc.usage,
            failed_returned_model=exc.returned_model,
            failed_diagnostics=exc.diagnostics,
            assignment=assignment,
        )
    return ExpertOutcome(
        perspective=perspective,
        elapsed_seconds=time.monotonic() - start,
        run=run,
        assignment=assignment,
    )


async def _run_experts(
    plan: str, experts: list[tuple[str, ExpertDefinition | None]]
) -> PipelineResult:
    start = time.monotonic()
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(_run_one(plan, perspective, assignment))
            for perspective, assignment in experts
        ]
    return PipelineResult(
        outcomes=[task.result() for task in tasks],
        elapsed_seconds=time.monotonic() - start,
    )


async def run_pipeline(plan: str, perspectives: list[str]) -> PipelineResult:
    """Run each perspective's expert concurrently over the same plan.

    All input (and the API key/model configuration) is validated before any
    expert starts; problems raise `ValueError`. An `ExpertError` in one
    expert becomes a failed outcome without affecting the others. Any other
    exception cancels the remaining experts and propagates (wrapped in an
    `ExceptionGroup` by `asyncio.TaskGroup`).
    """
    if not plan.strip():
        raise ValueError("plan must not be blank")
    names = _normalize_perspectives(perspectives)
    # Fail once, up front, rather than once per expert.
    get_anthropic_api_key()
    get_anthropic_model()

    return await _run_experts(plan, [(name, None) for name in names])


async def run_team(plan: str, team: TeamPlan) -> PipelineResult:
    """Run a validated team's experts concurrently, in team order.

    Same behavior as `run_pipeline`, except each expert also receives its own
    objective and questions. Perspectives are trimmed; `TeamPlan` has already
    rejected blanks and duplicates.
    """
    if not plan.strip():
        raise ValueError("plan must not be blank")
    return await _run_experts(plan, [(e.perspective.strip(), e) for e in team.experts])


# "completed": the synthesis call succeeded. "no_findings": experts succeeded but
# reported nothing, so an empty report is returned without a call. "unavailable":
# nothing to synthesize (team selection failed, or every expert failed).
# "failed": the synthesis call failed; expert reports are still returned.
SynthesisStatus = Literal["completed", "no_findings", "unavailable", "failed"]


@dataclass(frozen=True)
class SynthesisOutcome:
    status: SynthesisStatus
    reason: str | None = None
    elapsed_seconds: float | None = None
    run: SynthesisRun | None = None
    error: SynthesisError | None = None

    @property
    def report(self) -> SynthesisReport | None:
        if self.run is not None:
            return self.run.report
        return SynthesisReport(findings=[]) if self.status == "no_findings" else None

    @property
    def usage(self) -> UsageSummary | None:
        if self.run is not None:
            return self.run.usage
        return self.error.usage if self.error is not None else None

    @property
    def succeeded(self) -> bool:
        return self.status in ("completed", "no_findings")


@dataclass(frozen=True)
class AutoTeamRun:
    """An `--auto-team` run: selection (or its failure), experts, and synthesis."""

    directory: Path
    elapsed_seconds: float
    selection: TeamSelection | None = None
    selection_error: TeamSelectionError | None = None
    result: PipelineResult | None = None
    sources: dict[str, SourceRef] | None = None
    synthesis: SynthesisOutcome | None = None

    @property
    def status(self) -> PipelineStatus:
        return self.result.status if self.result is not None else "failed"

    @property
    def selector_usage(self) -> UsageSummary | None:
        if self.selection is not None:
            return self.selection.usage
        return self.selection_error.usage if self.selection_error is not None else None

    @property
    def succeeded(self) -> bool:
        """Every expert succeeded and synthesis produced a report (possibly empty)."""
        return self.status == "success" and self.synthesis is not None and self.synthesis.succeeded


async def _run_synthesis(plan: str, result: PipelineResult) -> SynthesisOutcome:
    experts = [
        ExpertInput(
            position=position,
            assignment=outcome.assignment,
            report=outcome.run.report if outcome.run is not None else None,
        )
        for position, outcome in enumerate(result.outcomes, start=1)
        if outcome.assignment is not None
    ]
    if all(e.report is None for e in experts):
        return SynthesisOutcome(status="unavailable", reason="all experts failed")
    if not source_map(experts):
        return SynthesisOutcome(
            status="no_findings",
            reason="successful expert reports contained no findings",
            elapsed_seconds=0.0,
        )
    start = time.monotonic()
    try:
        run = await synthesize(plan, experts)
    except SynthesisError as exc:
        return SynthesisOutcome(
            status="failed", elapsed_seconds=time.monotonic() - start, error=exc
        )
    return SynthesisOutcome(status="completed", elapsed_seconds=time.monotonic() - start, run=run)


async def run_auto_team(
    plan: str,
    *,
    on_directory: Callable[[Path], None] | None = None,
    on_team: Callable[[TeamSelection], None] | None = None,
) -> AutoTeamRun:
    """Select a team, run its experts concurrently, then synthesize their findings.

    Configuration and the plan are validated first (`ValueError`). Files under
    `runs/<id>/`: `team.json` (team or selection failure) before any expert
    starts; `experts.json` (expert outcomes and the source-ID mapping) before
    synthesis starts; `result.json` at the end. A failed selection starts no
    experts; a failed synthesis keeps the expert reports. Unexpected exceptions
    propagate after the already-saved files.
    """
    if not plan.strip():
        raise ValueError("plan must not be blank")
    get_anthropic_api_key()
    model = get_anthropic_model()

    start = time.monotonic()
    run_id, directory = create_run_dir("team-")
    if on_directory is not None:
        on_directory(directory)
    team_record: dict[str, Any] = {
        "run_id": run_id,
        "created_at": utc_now(),
        "plan": plan,
        "git": git_state(),
        "selector": {
            "requested_model": model,
            "system_prompt": TEAM_SELECTOR_SYSTEM_PROMPT,
            "catalogue": list(PERSPECTIVE_CATALOGUE),
            "max_output_tokens": TEAM_SELECTOR_MAX_OUTPUT_TOKENS,
        },
    }
    try:
        selection = await select_team(plan)
    except TeamSelectionError as exc:
        failed = AutoTeamRun(
            directory=directory,
            elapsed_seconds=time.monotonic() - start,
            selection_error=exc,
            synthesis=SynthesisOutcome(status="unavailable", reason="team selection failed"),
        )
        write_json(directory / "team.json", {**team_record, **_selection_json(failed)})
        return failed

    selected = AutoTeamRun(directory=directory, elapsed_seconds=0.0, selection=selection)
    write_json(directory / "team.json", {**team_record, **_selection_json(selected)})
    if on_team is not None:
        on_team(selection)

    result = await run_team(plan, selection.team)
    sources = source_map(
        [
            ExpertInput(position, o.assignment, o.run.report if o.run is not None else None)
            for position, o in enumerate(result.outcomes, start=1)
            if o.assignment is not None
        ]
    )
    expert_stage = AutoTeamRun(
        directory=directory,
        elapsed_seconds=time.monotonic() - start,
        selection=selection,
        result=result,
        sources=sources,
    )
    # Saved before synthesis starts, so it survives a synthesis failure or interruption.
    write_json(
        directory / "experts.json",
        {"run_id": run_id, "saved_at": utc_now(), "plan": plan, **auto_result_json(expert_stage)},
    )

    synthesis = await _run_synthesis(plan, result)
    auto = AutoTeamRun(
        directory=directory,
        elapsed_seconds=time.monotonic() - start,
        selection=selection,
        result=result,
        sources=sources,
        synthesis=synthesis,
    )
    write_json(
        directory / "result.json",
        {"run_id": run_id, "completed_at": utc_now(), "plan": plan, **auto_result_json(auto)},
    )
    return auto


def _usage_json(usage: UsageSummary | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "model_calls": usage.model_calls,
        "recovery_attempted": usage.recovery_attempted,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "usage_incomplete": usage.calls_missing_usage > 0,
    }


def _outcome_json(outcome: ExpertOutcome) -> dict[str, Any]:
    run = outcome.run
    assignment = (
        {"assignment": outcome.assignment.model_dump()} if outcome.assignment is not None else {}
    )
    return {
        "perspective": outcome.perspective,
        "status": outcome.status,
        "elapsed_seconds": round(outcome.elapsed_seconds, 3),
        "model": outcome.returned_model,
        "report": run.report.model_dump() if run is not None else None,
        "error": outcome.error,
        "diagnostics": (
            outcome.failed_diagnostics.to_json() if outcome.failed_diagnostics is not None else None
        ),
        **assignment,
        "usage": _usage_json(outcome.usage),
    }


def result_json(result: PipelineResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "experts": [_outcome_json(o) for o in result.outcomes],
    }


def _total_usage_json(usages: list[UsageSummary | None]) -> dict[str, Any]:
    known = [u for u in usages if u is not None]
    model_calls = sum(u.model_calls for u in known)
    missing = sum(u.calls_missing_usage for u in known)
    return {
        "model_calls": model_calls,
        "input_tokens": sum(u.input_tokens for u in known),
        "output_tokens": sum(u.output_tokens for u in known),
        "usage_incomplete": missing > 0,
    }


def _selection_json(auto: AutoTeamRun) -> dict[str, Any]:
    if auto.selection is not None:
        return {
            "team": auto.selection.team.model_dump(),
            "team_selection": {
                "status": "success",
                "model": auto.selection.model,
                "error": None,
                "diagnostics": None,
                "usage": _usage_json(auto.selection.usage),
            },
        }
    error = auto.selection_error
    assert error is not None
    return {
        "team": None,
        "team_selection": {
            "status": "failed",
            "model": error.returned_model,
            "error": str(error),
            "diagnostics": error.diagnostics.to_json() if error.diagnostics is not None else None,
            "usage": _usage_json(error.usage),
        },
    }


def _synthesis_json(synthesis: SynthesisOutcome) -> dict[str, Any]:
    error = synthesis.error
    report = synthesis.report
    return {
        "status": synthesis.status,
        "reason": synthesis.reason,
        "model": (
            synthesis.run.model
            if synthesis.run is not None
            else (error.returned_model if error is not None else None)
        ),
        "elapsed_seconds": (
            round(synthesis.elapsed_seconds, 3) if synthesis.elapsed_seconds is not None else None
        ),
        "report": report.model_dump() if report is not None else None,
        "error": str(error) if error is not None else None,
        "diagnostics": (
            error.diagnostics.to_json()
            if error is not None and error.diagnostics is not None
            else None
        ),
        "usage": _usage_json(synthesis.usage),
        "settings": {
            "system_prompt": SYNTHESIS_SYSTEM_PROMPT,
            "max_output_tokens": SYNTHESIS_MAX_OUTPUT_TOKENS,
        },
    }


def auto_result_json(auto: AutoTeamRun) -> dict[str, Any]:
    """The auto-team JSON. Omits `synthesis` until it has run (the expert-stage file)."""
    outcomes = auto.result.outcomes if auto.result is not None else []
    synthesis = auto.synthesis
    stage_usage = [auto.selector_usage, *(o.usage for o in outcomes)]
    if synthesis is not None:
        stage_usage.append(synthesis.usage)
    data: dict[str, Any] = {
        "status": auto.status,
        **_selection_json(auto),
        "experts": [_outcome_json(o) for o in outcomes],
    }
    if auto.sources is not None:
        data["sources"] = {sid: ref.to_json() for sid, ref in auto.sources.items()}
    if synthesis is not None:
        data["synthesis"] = _synthesis_json(synthesis)
    # Selector + experts (each already including its recovery) + synthesis, each once.
    data["total_usage"] = _total_usage_json(stage_usage)
    return data


def _incomplete_note(usages: list[UsageSummary]) -> str:
    missing = sum(u.calls_missing_usage for u in usages)
    calls = sum(u.model_calls for u in usages)
    return f" (incomplete: usage unavailable for {missing} of {calls} calls)" if missing else ""


def _print_summary(
    outcomes: list[ExpertOutcome],
    status: PipelineStatus,
    elapsed_seconds: float,
    selector_usage: UsageSummary | None = None,
    synthesis: SynthesisOutcome | None = None,
) -> None:
    expert_usages = [o.usage for o in outcomes if o.usage is not None]
    synthesis_usage = synthesis.usage if synthesis is not None else None
    usages = expert_usages + [u for u in (selector_usage, synthesis_usage) if u is not None]
    succeeded = sum(o.status == "success" for o in outcomes)
    model_calls = sum(u.model_calls for u in usages)
    recoveries = sum(u.recovery_attempted for u in expert_usages)
    note = _incomplete_note(usages)

    print(f"Status: {status} ({succeeded}/{len(outcomes)} experts succeeded)", file=sys.stderr)
    if selector_usage is not None:
        print(
            f"Team selector: {selector_usage.model_calls} call, "
            f"input tokens {selector_usage.input_tokens}, "
            f"output tokens {selector_usage.output_tokens}{_incomplete_note([selector_usage])}",
            file=sys.stderr,
        )
    if synthesis is not None:
        line = f"Synthesis: {synthesis.status}"
        if synthesis.report is not None:
            line += f" ({len(synthesis.report.findings)} findings)"
        if synthesis_usage is not None:
            line += (
                f", input tokens {synthesis_usage.input_tokens}, "
                f"output tokens {synthesis_usage.output_tokens}"
                f"{_incomplete_note([synthesis_usage])}"
            )
        elif synthesis.reason is not None:
            line += f" ({synthesis.reason})"
        print(line, file=sys.stderr)
    print(f"Model calls: {model_calls} (recoveries: {recoveries})", file=sys.stderr)
    print(f"Input tokens: {sum(u.input_tokens for u in usages)}{note}", file=sys.stderr)
    print(f"Output tokens: {sum(u.output_tokens for u in usages)}{note}", file=sys.stderr)
    print(f"Elapsed: {elapsed_seconds:.2f}s", file=sys.stderr)


def _print_team(selection: TeamSelection) -> None:
    print(f"Selected team ({len(selection.team.experts)} experts):", file=sys.stderr)
    for expert in selection.team.experts:
        print(f"- {expert.perspective.strip()}: {expert.objective}", file=sys.stderr)
        for question in expert.questions:
            print(f"    ? {question}", file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.pipeline",
        description="Run one to three Blindspot expert perspectives concurrently against a plan.",
    )
    parser.add_argument("--plan", required=True, help="The plan or decision to analyze.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--perspective",
        dest="perspectives",
        action="append",
        help=f"A perspective to analyze from (repeat, up to {MAX_PERSPECTIVES}).",
    )
    mode.add_argument(
        "--auto-team",
        action="store_true",
        help="Let one model call select the expert team (results saved under runs/).",
    )
    return parser.parse_args(argv)


async def _run(plan: str, perspectives: list[str]) -> int:
    try:
        result = await run_pipeline(plan, perspectives)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result_json(result), indent=2, ensure_ascii=False))
    _print_summary(result.outcomes, result.status, result.elapsed_seconds)
    return 0 if result.status == "success" else 1


async def _run_auto(plan: str) -> int:
    directories: list[Path] = []
    try:
        auto = await run_auto_team(plan, on_directory=directories.append, on_team=_print_team)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except BaseException:
        if directories:
            print(f"Run stopped; saved files are in: {directories[0]}", file=sys.stderr)
        raise

    print(json.dumps(auto_result_json(auto), indent=2, ensure_ascii=False))
    if auto.selection_error is not None:
        print(f"Error: {auto.selection_error}", file=sys.stderr)
    if auto.synthesis is not None and auto.synthesis.error is not None:
        print(f"Error: {auto.synthesis.error}", file=sys.stderr)
    outcomes = auto.result.outcomes if auto.result is not None else []
    _print_summary(outcomes, auto.status, auto.elapsed_seconds, auto.selector_usage, auto.synthesis)
    print(f"Results saved in: {auto.directory}", file=sys.stderr)
    return 0 if auto.succeeded else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.auto_team:
        return asyncio.run(_run_auto(args.plan))
    return asyncio.run(_run(args.plan, args.perspectives))


if __name__ == "__main__":
    sys.exit(main())
