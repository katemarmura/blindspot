"""Blindspot's first single-expert experiment: one perspective, one model call.

Run with:
    uv run python -m blindspot.expert --plan "..." --perspective "..."

This is a standalone experiment, not orchestration — there is no
ExpertDefinition abstraction, no multi-expert pipeline, and no persistence.

One bounded recovery step exists: if the model returns more than three
otherwise-valid findings, a single second call asks the same model to select
up to three of them by index. There is never a third call.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

import anthropic
import pydantic
from anthropic.types import Message, OutputConfigParam, TextBlock
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.prompts import EXPERT_SYSTEM_PROMPT, SELECTION_SYSTEM_PROMPT
from blindspot.schemas import ExpertReport, Finding

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_TOKENS = 3000
SELECTION_MAX_OUTPUT_TOKENS = 256
MAX_FINDINGS = 3


class _CandidateReport(BaseModel):
    """Internal: `ExpertReport` without the upper bound on findings.

    Used only to decide whether an over-long report is recoverable. Never
    returned to callers.
    """

    model_config = ConfigDict(extra="forbid")

    findings: list[Finding]


class _Selection(BaseModel):
    """Internal: the recovery call's output — indices into the candidate list."""

    model_config = ConfigDict(extra="forbid")

    selected_indices: list[StrictInt] = Field(min_length=0, max_length=MAX_FINDINGS)

    @field_validator("selected_indices")
    @classmethod
    def _check_unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("indices must be unique")
        return value


def request_output_schemas() -> dict[str, dict[str, Any]]:
    """The exact JSON schemas sent as structured-output formats, for recording experiments."""
    return {
        "expert_report": anthropic.transform_schema(ExpertReport),
        "selection": anthropic.transform_schema(_Selection),
    }


def _output_config(schema: type[BaseModel]) -> OutputConfigParam:
    # Same transformation `messages.parse(output_format=...)` applies, so the
    # request sent to the API is unchanged; validation is done locally instead,
    # which keeps the response (and its usage) when validation fails.
    return {"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}}


@dataclass
class UsageSummary:
    """Model calls made during one run and the token usage they returned.

    A call that was attempted but returned no usage (e.g. an API error)
    counts toward `model_calls` but not `calls_with_usage`; the token totals
    are then a lower bound, not the true total.
    """

    model_calls: int = 0
    calls_with_usage: int = 0
    recovery_attempted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def calls_missing_usage(self) -> int:
        return self.model_calls - self.calls_with_usage

    def record(self, response: Message) -> None:
        self.calls_with_usage += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens


class ExpertError(Exception):
    """Raised when an expert run fails: refusal, truncation, invalid output, or an API error.

    `usage` describes any model calls made before the failure (None if the
    run failed before making one). `returned_model` is the model ID reported
    by the initial response when one was received (None if the failure
    happened before any response, e.g. an API error on the initial call).
    """

    def __init__(
        self,
        message: str,
        usage: UsageSummary | None = None,
        returned_model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.returned_model = returned_model


def _describe_validation_error(exc: pydantic.ValidationError) -> str:
    """Summarize a ValidationError by field location and message only.

    Never includes `input_value` — pydantic's default error rendering embeds
    the raw value that failed validation, which here comes from model
    output and must not reach logs/stderr unsanitized.
    """
    details = exc.errors(include_url=False, include_context=False, include_input=False)
    parts = [f"{'.'.join(str(part) for part in d['loc'])}: {d['msg']}" for d in details]
    return "; ".join(parts) if parts else "no details available"


@dataclass(frozen=True)
class ExpertRun:
    report: ExpertReport
    model: str
    usage: UsageSummary


async def _call(
    client: anthropic.AsyncAnthropic,
    usage: UsageSummary,
    *,
    model: str,
    max_tokens: int,
    system: str,
    user_content: str,
    output_schema: type[BaseModel],
) -> Message:
    """Make one non-streaming model call, counting it and recording its usage if returned."""
    usage.model_calls += 1
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_config=_output_config(output_schema),
        )
    except anthropic.AuthenticationError as exc:
        raise ExpertError(f"Authentication error: {exc.message}", usage) from exc
    except anthropic.RateLimitError as exc:
        raise ExpertError(f"Rate-limit error: {exc.message}", usage) from exc
    except anthropic.APIConnectionError as exc:
        raise ExpertError(f"Connection error: {exc}", usage) from exc
    except anthropic.APIStatusError as exc:
        raise ExpertError(f"API error ({exc.status_code}): {exc.message}", usage) from exc
    usage.record(response)
    return response


def _check_stop_reason(response: Message, usage: UsageSummary, prefix: str = "") -> None:
    if response.stop_reason == "refusal":
        raise ExpertError(f"{prefix}The model declined to respond to this request.", usage)
    if response.stop_reason == "max_tokens":
        raise ExpertError(
            f"{prefix}The model response was truncated before completion "
            "(max output tokens reached).",
            usage,
        )


def _response_text(response: Message) -> str | None:
    # Mirrors `ParsedMessage.parsed_output`: the first text block holds the output.
    for block in response.content:
        if isinstance(block, TextBlock):
            return block.text
    return None


def _format_candidates(candidates: list[Finding]) -> str:
    labelled = [{"index": i, **finding.model_dump()} for i, finding in enumerate(candidates)]
    return json.dumps(labelled, indent=2, ensure_ascii=False)


async def _select_findings(
    client: anthropic.AsyncAnthropic,
    model: str,
    plan: str,
    perspective: str,
    candidates: list[Finding],
    usage: UsageSummary,
) -> ExpertReport:
    """The one recovery call: ask the model to pick up to three candidates by index."""
    usage.recovery_attempted = True
    user_content = (
        f"Plan:\n{plan}\n\nPerspective:\n{perspective}\n\n"
        f"Candidate findings:\n{_format_candidates(candidates)}"
    )
    response = await _call(
        client,
        usage,
        model=model,
        max_tokens=SELECTION_MAX_OUTPUT_TOKENS,
        system=SELECTION_SYSTEM_PROMPT,
        user_content=user_content,
        output_schema=_Selection,
    )
    _check_stop_reason(response, usage, prefix="Finding selection failed: ")

    text = _response_text(response)
    if text is None:
        raise ExpertError("Finding selection returned no output.", usage)
    try:
        selection = _Selection.model_validate_json(text)
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        raise ExpertError(f"Finding selection was invalid ({details})", usage) from exc

    if any(not 0 <= i < len(candidates) for i in selection.selected_indices):
        raise ExpertError(
            f"Finding selection was invalid (index out of range for {len(candidates)} candidates)",
            usage,
        )

    # Built from the original Finding objects, so wording is preserved exactly.
    try:
        return ExpertReport(findings=[candidates[i] for i in selection.selected_indices])
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        raise ExpertError(f"Selected report was invalid ({details})", usage) from exc


def _recoverable_candidates(response: Message, text: str) -> list[Finding] | None:
    """Return the candidate findings if the only problem is too many of them.

    Recovery applies only to a normally completed response whose findings
    all satisfy `Finding` and whose sole schema violation is the count.
    """
    if response.stop_reason != "end_turn":
        return None
    try:
        candidates = _CandidateReport.model_validate_json(text).findings
    except pydantic.ValidationError:
        return None
    return candidates if len(candidates) > MAX_FINDINGS else None


async def _report_from_response(
    client: anthropic.AsyncAnthropic,
    model: str,
    plan: str,
    perspective: str,
    response: Message,
    usage: UsageSummary,
) -> ExpertReport:
    """Validate the initial response, running the bounded recovery if it applies."""
    _check_stop_reason(response, usage)

    text = _response_text(response)
    if text is None:
        raise ExpertError("The model did not return output matching the expected schema.", usage)

    try:
        return ExpertReport.model_validate_json(text)
    except pydantic.ValidationError as exc:
        candidates = _recoverable_candidates(response, text)
        if candidates is None:
            details = _describe_validation_error(exc)
            raise ExpertError(
                f"Model output did not match the expected schema ({details})", usage
            ) from exc
    return await _select_findings(client, model, plan, perspective, candidates, usage)


async def run_expert(plan: str, perspective: str, *, model: str | None = None) -> ExpertRun:
    """Run one expert perspective over one plan and return a validated result.

    `model` overrides `ANTHROPIC_MODEL` for both the initial and any recovery
    call; when given, `ANTHROPIC_MODEL` need not be configured. Omitted, the
    configured model is used as before.

    An `ExpertReport` with zero findings is a successful result, not an
    error. If the model returns more than three findings that are otherwise
    valid, one additional selection call picks up to three of them (see the
    module docstring). Raises `ValueError` for a blank `plan`/`perspective`/
    `model`, or `ExpertError` for a refusal, truncated response, output that fails
    `ExpertReport` validation (and is not recoverable), a failed recovery,
    or an Anthropic API error.
    """
    if not plan.strip():
        raise ValueError("plan must not be blank")
    if not perspective.strip():
        raise ValueError("perspective must not be blank")
    if model is not None and not model.strip():
        raise ValueError("model must not be blank")

    api_key = get_anthropic_api_key()
    model = model.strip() if model is not None else get_anthropic_model()
    usage = UsageSummary()

    user_content = f"Plan:\n{plan}\n\nPerspective:\n{perspective}"

    async with anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    ) as client:
        response = await _call(
            client,
            usage,
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=EXPERT_SYSTEM_PROMPT,
            user_content=user_content,
            output_schema=ExpertReport,
        )
        try:
            report = await _report_from_response(client, model, plan, perspective, response, usage)
        except ExpertError as exc:
            # A response was received, so its model ID is known even though the
            # run failed (same source as `ExpertRun.model` on success).
            exc.returned_model = response.model
            raise

    return ExpertRun(report=report, model=response.model, usage=usage)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.expert",
        description="Run a single Blindspot expert perspective against a plan.",
    )
    parser.add_argument("--plan", required=True, help="The plan or decision to analyze.")
    parser.add_argument("--perspective", required=True, help="The perspective to analyze it from.")
    return parser.parse_args(argv)


def _print_usage(usage: UsageSummary) -> None:
    print(f"Recovery attempted: {'yes' if usage.recovery_attempted else 'no'}", file=sys.stderr)
    print(f"Model calls: {usage.model_calls}", file=sys.stderr)
    if usage.calls_missing_usage:
        note = (
            f" (incomplete: usage unavailable for {usage.calls_missing_usage} "
            f"of {usage.model_calls} calls)"
        )
    else:
        note = ""
    print(f"Input tokens: {usage.input_tokens}{note}", file=sys.stderr)
    print(f"Output tokens: {usage.output_tokens}{note}", file=sys.stderr)


async def _run(plan: str, perspective: str) -> int:
    try:
        run = await run_expert(plan, perspective)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ExpertError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if exc.usage is not None:
            _print_usage(exc.usage)
        return 1

    print(run.report.model_dump_json(indent=2))
    print(f"Model: {run.model}", file=sys.stderr)
    _print_usage(run.usage)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args.plan, args.perspective))


if __name__ == "__main__":
    sys.exit(main())
