"""Minimal model comparison: one plan and perspective, two explicit models.

Run with:
    uv run python -m blindspot.compare --plan "..." --perspective "..." \\
        --model "<first-model-id>" --model "<second-model-id>" [--repeats 1-3]

Each repetition runs the unchanged single-expert call (`run_expert`) once per
model, sequentially, alternating which model goes first. Every run is saved
under `runs/<comparison-id>/` as soon as it completes, for manual inspection.
There is no automatic quality judgement, cost calculation, or retry.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from blindspot.config import get_anthropic_api_key
from blindspot.expert import (
    MAX_FINDINGS,
    MAX_OUTPUT_TOKENS,
    REQUEST_TIMEOUT_SECONDS,
    SELECTION_MAX_OUTPUT_TOKENS,
    ExpertError,
    ExpertRun,
    FailureDiagnostics,
    UsageSummary,
    request_output_schemas,
    run_expert,
)
from blindspot.prompts import EXPERT_SYSTEM_PROMPT, SELECTION_SYSTEM_PROMPT

RUNS_DIR = Path("runs")
MIN_REPEATS = 1
MAX_REPEATS = 3


@dataclass(frozen=True)
class ComparisonConfig:
    plan: str
    perspective: str
    models: tuple[str, str]
    repeats: int


@dataclass(frozen=True)
class RunRecord:
    repetition: int
    execution_order: int
    requested_model: str
    started_at: str
    elapsed_seconds: float
    run: ExpertRun | None = None
    error: str | None = None
    failed_usage: UsageSummary | None = None
    # On failure, the model ID from a received response, if any.
    failed_returned_model: str | None = None
    # On failure, structured diagnostics from the ExpertError; None on success.
    failed_diagnostics: FailureDiagnostics | None = None

    @property
    def status(self) -> Literal["success", "failed"]:
        return "success" if self.run is not None else "failed"

    @property
    def returned_model(self) -> str | None:
        return self.run.model if self.run is not None else self.failed_returned_model

    @property
    def usage(self) -> UsageSummary | None:
        return self.run.usage if self.run is not None else self.failed_usage


@dataclass(frozen=True)
class Comparison:
    directory: Path
    config: ComparisonConfig
    records: list[RunRecord]


def validate_config(
    plan: str, perspective: str, models: list[str], repeats: int
) -> ComparisonConfig:
    """Check every argument before anything is created or sent. Raises `ValueError`."""
    if not plan.strip():
        raise ValueError("plan must not be blank")
    if not perspective.strip():
        raise ValueError("perspective must not be blank")
    if len(models) != 2:
        raise ValueError(f"exactly two --model values are required (got {len(models)})")
    first, second = (m.strip() for m in models)
    if not first or not second:
        raise ValueError("model IDs must not be blank")
    if first == second:
        raise ValueError("the two model IDs must be different")
    if not MIN_REPEATS <= repeats <= MAX_REPEATS:
        raise ValueError(f"--repeats must be between {MIN_REPEATS} and {MAX_REPEATS}")
    return ComparisonConfig(
        plan=plan, perspective=perspective, models=(first, second), repeats=repeats
    )


def run_order(config: ComparisonConfig) -> list[tuple[int, str]]:
    """(repetition, model) pairs in execution order; the first model alternates."""
    first, second = config.models
    order: list[tuple[int, str]] = []
    for repetition in range(1, config.repeats + 1):
        pair = (first, second) if repetition % 2 else (second, first)
        order.extend((repetition, model) for model in pair)
    return order


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def _git_state() -> dict[str, Any]:
    revision = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain") if revision is not None else None
    return {
        "revision": revision.strip() if revision is not None else None,
        "dirty": bool(status.strip()) if status is not None else None,
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    # Write to a temporary file then rename, so an interruption never leaves
    # a truncated result file behind.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _manifest(config: ComparisonConfig, comparison_id: str) -> dict[str, Any]:
    return {
        "comparison_id": comparison_id,
        "created_at": _utc_now(),
        "plan": config.plan,
        "perspective": config.perspective,
        "models": list(config.models),
        "repeats": config.repeats,
        "prompts": {
            "expert_system": EXPERT_SYSTEM_PROMPT,
            "selection_system": SELECTION_SYSTEM_PROMPT,
        },
        "output_schemas": request_output_schemas(),
        "request_limits": {
            "max_findings": MAX_FINDINGS,
            "expert_max_output_tokens": MAX_OUTPUT_TOKENS,
            "selection_max_output_tokens": SELECTION_MAX_OUTPUT_TOKENS,
            "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "sdk_max_retries": 0,
            "max_model_calls_per_run": 2,
        },
        "git": _git_state(),
    }


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


def _record_json(record: RunRecord) -> dict[str, Any]:
    run = record.run
    return {
        "repetition": record.repetition,
        "execution_order": record.execution_order,
        "requested_model": record.requested_model,
        "returned_model": record.returned_model,
        "status": record.status,
        "started_at": record.started_at,
        "elapsed_seconds": round(record.elapsed_seconds, 3),
        "report": run.report.model_dump() if run is not None else None,
        "error": record.error,
        "diagnostics": (
            record.failed_diagnostics.to_json() if record.failed_diagnostics is not None else None
        ),
        "usage": _usage_json(record.usage),
    }


def _record_filename(record: RunRecord) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", record.requested_model)
    return f"run-{record.execution_order:02d}-rep{record.repetition}-{slug}.json"


async def _run_one(
    config: ComparisonConfig, repetition: int, execution_order: int, model: str
) -> RunRecord:
    started_at = _utc_now()
    start = time.monotonic()
    try:
        run = await run_expert(config.plan, config.perspective, model=model)
    except ExpertError as exc:
        # Only the expected, sanitized failure is recorded; anything else
        # (a bug, or cancellation) propagates.
        return RunRecord(
            repetition=repetition,
            execution_order=execution_order,
            requested_model=model,
            started_at=started_at,
            elapsed_seconds=time.monotonic() - start,
            error=str(exc),
            failed_usage=exc.usage,
            failed_returned_model=exc.returned_model,
            failed_diagnostics=exc.diagnostics,
        )
    return RunRecord(
        repetition=repetition,
        execution_order=execution_order,
        requested_model=model,
        started_at=started_at,
        elapsed_seconds=time.monotonic() - start,
        run=run,
    )


def create_comparison_dir(runs_dir: Path = RUNS_DIR) -> tuple[str, Path]:
    comparison_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    directory = runs_dir / comparison_id
    directory.mkdir(parents=True, exist_ok=False)
    return comparison_id, directory


async def run_comparison(
    config: ComparisonConfig,
    directory: Path,
    comparison_id: str,
    on_record: Callable[[RunRecord, int], None] | None = None,
) -> Comparison:
    """Run every (repetition, model) pair sequentially, saving each as it completes."""
    _write_json(directory / "manifest.json", _manifest(config, comparison_id))
    order = run_order(config)
    records: list[RunRecord] = []
    for execution_order, (repetition, model) in enumerate(order, start=1):
        record = await _run_one(config, repetition, execution_order, model)
        _write_json(directory / _record_filename(record), _record_json(record))
        records.append(record)
        if on_record is not None:
            on_record(record, len(order))
    return Comparison(directory=directory, config=config, records=records)


def _summary_lines(comparison: Comparison) -> list[str]:
    lines: list[str] = []
    for model in comparison.config.models:
        records = [r for r in comparison.records if r.requested_model == model]
        usages = [r.usage for r in records if r.usage is not None]
        succeeded = sum(r.status == "success" for r in records)
        recoveries = sum(u.recovery_attempted for u in usages)
        # A failure before any call (usage None) or a call without usage both
        # leave the totals incomplete.
        incomplete = any(r.usage is None for r in records) or any(
            u.calls_missing_usage for u in usages
        )
        note = " (incomplete)" if incomplete else ""
        input_tokens = sum(u.input_tokens for u in usages)
        output_tokens = sum(u.output_tokens for u in usages)
        average = sum(r.elapsed_seconds for r in records) / len(records) if records else 0.0
        lines += [
            f"{model}:",
            f"  runs: {succeeded} succeeded, {len(records) - succeeded} failed",
            f"  runs requiring recovery: {recoveries}",
            f"  input tokens: {input_tokens}{note}",
            f"  output tokens: {output_tokens}{note}",
            f"  average elapsed: {average:.2f}s",
        ]
    lines.append(f"Results saved in: {comparison.directory}")
    return lines


def _progress(record: RunRecord, total: int) -> None:
    print(
        f"[{record.execution_order}/{total}] rep {record.repetition} "
        f"{record.requested_model}: {record.status} ({record.elapsed_seconds:.1f}s)",
        file=sys.stderr,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.compare",
        description="Run one plan and perspective through two models for manual comparison.",
    )
    parser.add_argument("--plan", required=True, help="The plan or decision to analyze.")
    parser.add_argument("--perspective", required=True, help="The perspective to analyze it from.")
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        required=True,
        help="A model ID to compare (give exactly two).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help=f"Repetitions per model, {MIN_REPEATS}-{MAX_REPEATS} (default 1).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    try:
        config = validate_config(args.plan, args.perspective, args.models, args.repeats)
        get_anthropic_api_key()  # fail before creating anything if unconfigured
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    comparison_id, directory = create_comparison_dir()
    try:
        comparison = await run_comparison(config, directory, comparison_id, on_record=_progress)
    except BaseException:
        print(f"Comparison stopped; completed runs are saved in: {directory}", file=sys.stderr)
        raise

    print("\n".join(_summary_lines(comparison)))
    return 0 if all(r.status == "success" for r in comparison.records) else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
