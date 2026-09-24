"""Blindspot's first single-expert experiment: one perspective, one model call.

Run with:
    uv run python -m blindspot.expert --plan "..." --perspective "..."

`run_expert` is also what `blindspot.pipeline` runs per expert. It
optionally takes an assigned objective and investigation questions (from an
automatically selected team); without them it behaves as a plain
plan/perspective call.

One bounded recovery step exists: if the model returns more than three
otherwise-valid findings, a single second call asks the same model to select
up to three of them by index. There is never a third call.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
import pydantic
from anthropic.types import Message, OutputConfigParam, TextBlock
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.prompts import EXPERT_SYSTEM_PROMPT, SELECTION_SYSTEM_PROMPT
from blindspot.schemas import ExpertDefinition, ExpertReport, Finding, TeamPlan

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


# "initial"/"selection" are an expert's own calls (report, finding-selection
# recovery); "team_selection" is the pipeline's team-selector call.
FailureStage = Literal["initial", "selection", "team_selection"]

# Why the bounded recovery was not attempted after an initial-stage failure.
RecoverySkipReason = Literal[
    "no_response",  # the API call failed before any response arrived
    "response_not_end_turn",  # the response did not end normally (see stop_reason)
    "no_text_output",  # the response had no text block
    "malformed_json",  # the text was not valid JSON
    "candidate_validation_failed",  # invalid beyond the finding-count limit
    "within_finding_limit",  # defensive: no count violation to recover from
]

ValidationSchema = Literal["expert_report", "candidate_report", "selection", "team_plan"]


@dataclass(frozen=True)
class ValidationIssue:
    """One sanitized validation error: which schema, where, and pydantic's error type."""

    schema: ValidationSchema
    loc: str
    type: str


@dataclass(frozen=True)
class FailureDiagnostics:
    """Structured, content-free facts about why an expert run failed.

    Fields that don't apply are None. Built from the failure itself, never by
    parsing the human-readable error message.
    """

    failure_stage: FailureStage
    stop_reason: str | None = None
    recovery_skip_reason: RecoverySkipReason | None = None
    validation_errors: tuple[ValidationIssue, ...] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "failure_stage": self.failure_stage,
            "stop_reason": self.stop_reason,
            "recovery_skip_reason": self.recovery_skip_reason,
            "validation_errors": (
                [{"schema": i.schema, "loc": i.loc, "type": i.type} for i in self.validation_errors]
                if self.validation_errors is not None
                else None
            ),
        }


class ExpertError(Exception):
    """Raised when an expert run fails: refusal, truncation, invalid output, or an API error.

    `usage` describes any model calls made before the failure (None if the
    run failed before making one). `returned_model` is the model ID reported
    by the initial response when one was received (None if the failure
    happened before any response, e.g. an API error on the initial call).
    `diagnostics` gives the failing stage, stop reason, why recovery was
    skipped, and sanitized validation errors (set by `run_expert`).
    """

    def __init__(
        self,
        message: str,
        usage: UsageSummary | None = None,
        returned_model: str | None = None,
        diagnostics: FailureDiagnostics | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.returned_model = returned_model
        self.diagnostics = diagnostics


# Field names defined by our own schemas. Any other name in an error location
# (e.g. an unexpected key the model emitted) is model-generated and replaced.
_KNOWN_FIELDS = frozenset(
    {
        *ExpertReport.model_fields,
        *Finding.model_fields,
        *_Selection.model_fields,
        *TeamPlan.model_fields,
        *ExpertDefinition.model_fields,
    }
)
_UNKNOWN_FIELD = "<unknown field>"


def _sanitize_loc(loc: tuple[int | str, ...]) -> str:
    parts = [
        str(part) if isinstance(part, int) or part in _KNOWN_FIELDS else _UNKNOWN_FIELD
        for part in loc
    ]
    return ".".join(parts) if parts else "<root>"


def _errors(exc: pydantic.ValidationError) -> list[Any]:
    # Never include `input` — it holds raw model output.
    return exc.errors(include_url=False, include_context=False, include_input=False)


def _describe_validation_error(exc: pydantic.ValidationError) -> str:
    """Summarize a ValidationError by sanitized field location and message only."""
    parts = [f"{_sanitize_loc(d['loc'])}: {d['msg']}" for d in _errors(exc)]
    return "; ".join(parts) if parts else "no details available"


def _validation_issues(
    schema: ValidationSchema, exc: pydantic.ValidationError
) -> tuple[ValidationIssue, ...]:
    return tuple(
        ValidationIssue(schema=schema, loc=_sanitize_loc(d["loc"]), type=d["type"])
        for d in _errors(exc)
    )


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
    stage: FailureStage,
) -> Message:
    """Make one non-streaming model call, counting it and recording its usage if returned."""
    usage.model_calls += 1
    diagnostics = FailureDiagnostics(
        failure_stage=stage,
        recovery_skip_reason="no_response" if stage == "initial" else None,
    )
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_config=_output_config(output_schema),
        )
    except anthropic.AuthenticationError as exc:
        message = f"Authentication error: {exc.message}"
        raise ExpertError(message, usage, diagnostics=diagnostics) from exc
    except anthropic.RateLimitError as exc:
        message = f"Rate-limit error: {exc.message}"
        raise ExpertError(message, usage, diagnostics=diagnostics) from exc
    except anthropic.APIConnectionError as exc:
        raise ExpertError(f"Connection error: {exc}", usage, diagnostics=diagnostics) from exc
    except anthropic.APIStatusError as exc:
        message = f"API error ({exc.status_code}): {exc.message}"
        raise ExpertError(message, usage, diagnostics=diagnostics) from exc
    usage.record(response)
    return response


def _check_stop_reason(
    response: Message, usage: UsageSummary, stage: FailureStage, prefix: str = ""
) -> None:
    diagnostics = FailureDiagnostics(
        failure_stage=stage,
        stop_reason=response.stop_reason,
        recovery_skip_reason="response_not_end_turn" if stage == "initial" else None,
    )
    if response.stop_reason == "refusal":
        raise ExpertError(
            f"{prefix}The model declined to respond to this request.",
            usage,
            diagnostics=diagnostics,
        )
    if response.stop_reason == "max_tokens":
        raise ExpertError(
            f"{prefix}The model response was truncated before completion "
            "(max output tokens reached).",
            usage,
            diagnostics=diagnostics,
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
    expert_input: str,
    candidates: list[Finding],
    usage: UsageSummary,
) -> ExpertReport:
    """The one recovery call: ask the model to pick up to three candidates by index.

    `expert_input` is the initial call's exact user content (plan, perspective,
    and any assigned objective and questions), so selection judges candidates
    against the same assignment the expert was given.
    """
    usage.recovery_attempted = True
    user_content = f"{expert_input}\n\nCandidate findings:\n{_format_candidates(candidates)}"
    response = await _call(
        client,
        usage,
        model=model,
        max_tokens=SELECTION_MAX_OUTPUT_TOKENS,
        system=SELECTION_SYSTEM_PROMPT,
        user_content=user_content,
        output_schema=_Selection,
        stage="selection",
    )
    _check_stop_reason(response, usage, "selection", prefix="Finding selection failed: ")

    def failure(message: str, issues: tuple[ValidationIssue, ...] | None = None) -> ExpertError:
        diagnostics = FailureDiagnostics(
            failure_stage="selection",
            stop_reason=response.stop_reason,
            validation_errors=issues,
        )
        return ExpertError(message, usage, diagnostics=diagnostics)

    text = _response_text(response)
    if text is None:
        raise failure("Finding selection returned no output.")
    try:
        selection = _Selection.model_validate_json(text)
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        issues = _validation_issues("selection", exc)
        raise failure(f"Finding selection was invalid ({details})", issues) from exc

    out_of_range = tuple(
        ValidationIssue(
            schema="selection", loc=f"selected_indices.{pos}", type="index_out_of_range"
        )
        for pos, i in enumerate(selection.selected_indices)
        if not 0 <= i < len(candidates)
    )
    if out_of_range:
        raise failure(
            f"Finding selection was invalid (index out of range for {len(candidates)} candidates)",
            out_of_range,
        )

    # Built from the original Finding objects, so wording is preserved exactly.
    try:
        return ExpertReport(findings=[candidates[i] for i in selection.selected_indices])
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        issues = _validation_issues("expert_report", exc)
        raise failure(f"Selected report was invalid ({details})", issues) from exc


def _recovery_skip_reason(
    response: Message,
    candidates: list[Finding] | None,
    candidate_error: pydantic.ValidationError | None,
) -> RecoverySkipReason | None:
    """Why recovery can't run, or None if it can.

    Recovery applies only to a normally completed response whose findings
    all satisfy `Finding` and whose sole schema violation is the count.
    """
    if response.stop_reason != "end_turn":
        return "response_not_end_turn"
    if candidate_error is not None:
        if any(d["type"] == "json_invalid" for d in _errors(candidate_error)):
            return "malformed_json"
        return "candidate_validation_failed"
    if candidates is None or len(candidates) <= MAX_FINDINGS:
        return "within_finding_limit"
    return None


async def _report_from_response(
    client: anthropic.AsyncAnthropic,
    model: str,
    expert_input: str,
    response: Message,
    usage: UsageSummary,
) -> ExpertReport:
    """Validate the initial response, running the bounded recovery if it applies."""
    _check_stop_reason(response, usage, "initial")

    text = _response_text(response)
    if text is None:
        raise ExpertError(
            "The model did not return output matching the expected schema.",
            usage,
            diagnostics=FailureDiagnostics(
                failure_stage="initial",
                stop_reason=response.stop_reason,
                recovery_skip_reason="no_text_output",
            ),
        )

    try:
        return ExpertReport.model_validate_json(text)
    except pydantic.ValidationError as exc:
        report_error = exc

    # The bounded `ExpertReport` stops at the count violation and hides
    # per-finding errors, so validate without the limit to see them too.
    candidates: list[Finding] | None = None
    candidate_error: pydantic.ValidationError | None = None
    try:
        candidates = _CandidateReport.model_validate_json(text).findings
    except pydantic.ValidationError as exc:
        candidate_error = exc

    skip_reason = _recovery_skip_reason(response, candidates, candidate_error)
    if skip_reason is None and candidates is not None:
        return await _select_findings(client, model, expert_input, candidates, usage)

    issues = _validation_issues("expert_report", report_error)
    report_details = _describe_validation_error(report_error)
    message = f"Model output did not match the expected schema ({report_details})"
    if candidate_error is not None:
        issues += _validation_issues("candidate_report", candidate_error)
        # Skip repeating an identical summary (e.g. malformed JSON fails both the same way).
        candidate_details = _describe_validation_error(candidate_error)
        if candidate_details != report_details:
            message += f"; candidate findings also invalid ({candidate_details})"
    message += f"; recovery not attempted: {skip_reason}"
    raise ExpertError(
        message,
        usage,
        diagnostics=FailureDiagnostics(
            failure_stage="initial",
            stop_reason=response.stop_reason,
            recovery_skip_reason=skip_reason,
            validation_errors=issues,
        ),
    ) from report_error


def _user_content(
    plan: str, perspective: str, objective: str | None, questions: list[str] | None
) -> str:
    content = f"Plan:\n{plan}\n\nPerspective:\n{perspective}"
    if objective is not None:
        content += f"\n\nObjective:\n{objective}"
    if questions is not None:
        numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
        content += f"\n\nInvestigation questions:\n{numbered}"
    return content


async def run_expert(
    plan: str,
    perspective: str,
    *,
    model: str | None = None,
    objective: str | None = None,
    questions: list[str] | None = None,
) -> ExpertRun:
    """Run one expert perspective over one plan and return a validated result.

    `model` overrides `ANTHROPIC_MODEL` for both the initial and any recovery
    call; when given, `ANTHROPIC_MODEL` need not be configured. Omitted, the
    configured model is used as before.

    `objective` and `questions` (an assignment from a selected team) are added
    to the expert's input as labelled sections when given. Omitted, the input
    is exactly the plan and perspective, as for manual runs.

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
    if objective is not None and not objective.strip():
        raise ValueError("objective must not be blank")
    if questions is not None and (not questions or any(not q.strip() for q in questions)):
        raise ValueError("questions must be a non-empty list of nonblank strings")

    api_key = get_anthropic_api_key()
    model = model.strip() if model is not None else get_anthropic_model()
    usage = UsageSummary()

    user_content = _user_content(plan, perspective, objective, questions)

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
            stage="initial",
        )
        try:
            report = await _report_from_response(client, model, user_content, response, usage)
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


def _print_diagnostics(diagnostics: FailureDiagnostics) -> None:
    def show(value: object) -> str:
        return "n/a" if value is None else str(value)

    print(f"Failure stage: {diagnostics.failure_stage}", file=sys.stderr)
    print(f"Stop reason: {show(diagnostics.stop_reason)}", file=sys.stderr)
    print(f"Recovery skip reason: {show(diagnostics.recovery_skip_reason)}", file=sys.stderr)
    if diagnostics.validation_errors:
        issues = "; ".join(f"{i.schema}:{i.loc} ({i.type})" for i in diagnostics.validation_errors)
        print(f"Validation errors: {issues}", file=sys.stderr)


async def _run(plan: str, perspective: str) -> int:
    try:
        run = await run_expert(plan, perspective)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ExpertError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if exc.diagnostics is not None:
            _print_diagnostics(exc.diagnostics)
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
