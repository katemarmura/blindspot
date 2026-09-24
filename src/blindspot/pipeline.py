"""Blindspot's first multi-expert runner: explicit perspectives, run concurrently.

Run with:
    uv run python -m blindspot.pipeline --plan "..." \\
        --perspective "Financial" --perspective "Operations"

This is not an LLM orchestrator. The caller supplies one to three perspectives;
each runs the existing single-expert call (`run_expert`) independently and
concurrently, seeing only the original plan and its own perspective. Outcomes
are returned per expert, in input order — findings are never merged, ranked,
or deduplicated, and there are no pipeline-level retries.
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.expert import ExpertError, ExpertRun, UsageSummary, run_expert

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


async def _run_one(plan: str, perspective: str) -> ExpertOutcome:
    start = time.monotonic()
    try:
        run = await run_expert(plan, perspective)
    except ExpertError as exc:
        # Only the expected, already-sanitized failure is captured. Anything
        # else (a bug, or cancellation) propagates to the TaskGroup.
        return ExpertOutcome(
            perspective=perspective,
            elapsed_seconds=time.monotonic() - start,
            error=str(exc),
            failed_usage=exc.usage,
            failed_returned_model=exc.returned_model,
        )
    return ExpertOutcome(perspective=perspective, elapsed_seconds=time.monotonic() - start, run=run)


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

    start = time.monotonic()
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(_run_one(plan, name)) for name in names]
    return PipelineResult(
        outcomes=[task.result() for task in tasks],
        elapsed_seconds=time.monotonic() - start,
    )


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
    return {
        "perspective": outcome.perspective,
        "status": outcome.status,
        "elapsed_seconds": round(outcome.elapsed_seconds, 3),
        "model": outcome.returned_model,
        "report": run.report.model_dump() if run is not None else None,
        "error": outcome.error,
        "usage": _usage_json(outcome.usage),
    }


def result_json(result: PipelineResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "experts": [_outcome_json(o) for o in result.outcomes],
    }


def _print_summary(result: PipelineResult) -> None:
    usages = [o.usage for o in result.outcomes if o.usage is not None]
    succeeded = sum(o.status == "success" for o in result.outcomes)
    model_calls = sum(u.model_calls for u in usages)
    recoveries = sum(u.recovery_attempted for u in usages)
    missing = sum(u.calls_missing_usage for u in usages)
    input_tokens = sum(u.input_tokens for u in usages)
    output_tokens = sum(u.output_tokens for u in usages)

    note = (
        f" (incomplete: usage unavailable for {missing} of {model_calls} calls)" if missing else ""
    )
    print(
        f"Status: {result.status} ({succeeded}/{len(result.outcomes)} experts succeeded)",
        file=sys.stderr,
    )
    print(f"Model calls: {model_calls} (recoveries: {recoveries})", file=sys.stderr)
    print(f"Input tokens: {input_tokens}{note}", file=sys.stderr)
    print(f"Output tokens: {output_tokens}{note}", file=sys.stderr)
    print(f"Elapsed: {result.elapsed_seconds:.2f}s", file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.pipeline",
        description="Run one to three Blindspot expert perspectives concurrently against a plan.",
    )
    parser.add_argument("--plan", required=True, help="The plan or decision to analyze.")
    parser.add_argument(
        "--perspective",
        dest="perspectives",
        action="append",
        required=True,
        help=f"A perspective to analyze from (repeat, up to {MAX_PERSPECTIVES}).",
    )
    return parser.parse_args(argv)


async def _run(plan: str, perspectives: list[str]) -> int:
    try:
        result = await run_pipeline(plan, perspectives)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result_json(result), indent=2, ensure_ascii=False))
    _print_summary(result)
    return 0 if result.status == "success" else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args.plan, args.perspectives))


if __name__ == "__main__":
    sys.exit(main())
