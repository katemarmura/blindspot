"""Synthesis: turn successful expert reports into a few distinct, cited considerations.

One structured-output call to the configured model (60s, 3000 output tokens,
no retries, no repair). Each supplied expert finding gets a run-local ID,
`e<team position>-f<finding position>` (both 1-based), and every synthesized
finding must cite at least one of those IDs. IDs are checked in Python: an
unknown ID is a synthesis failure. A valid ID shows which findings were
cited, not that the rewritten text faithfully represents them.

The original expert reports are never modified.
"""

import json
from dataclasses import dataclass
from typing import Any

import anthropic
import pydantic

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.expert import (
    REQUEST_TIMEOUT_SECONDS,
    ExpertError,
    FailureDiagnostics,
    UsageSummary,
    ValidationIssue,
    _call,
    _check_stop_reason,
    _describe_validation_error,
    _response_text,
    _validation_issues,
)
from blindspot.prompts import SYNTHESIS_SYSTEM_PROMPT
from blindspot.schemas import ExpertDefinition, ExpertReport, Finding, SynthesisReport

SYNTHESIS_MAX_OUTPUT_TOKENS = 3000


class SynthesisError(ExpertError):
    """The synthesis call failed; carries usage, returned model, and diagnostics."""


@dataclass(frozen=True)
class ExpertInput:
    """One team member's contribution to synthesis.

    `position` is the expert's 1-based position in the selected team (stable
    even when earlier experts failed). `report` is None if the expert failed.
    """

    position: int
    assignment: ExpertDefinition
    report: ExpertReport | None


@dataclass(frozen=True)
class SourceRef:
    """Where a source finding ID points: the expert's team position and finding position."""

    expert_position: int
    perspective: str
    finding_position: int
    finding: Finding

    def to_json(self) -> dict[str, Any]:
        return {
            "expert_position": self.expert_position,
            "perspective": self.perspective,
            "finding_position": self.finding_position,
            "title": self.finding.title,
        }


def source_map(experts: list[ExpertInput]) -> dict[str, SourceRef]:
    """Deterministic ID -> source mapping over successful experts' findings, in team order."""
    sources: dict[str, SourceRef] = {}
    for expert in experts:
        if expert.report is None:
            continue
        for j, finding in enumerate(expert.report.findings, start=1):
            sources[f"e{expert.position}-f{j}"] = SourceRef(
                expert_position=expert.position,
                perspective=expert.assignment.perspective.strip(),
                finding_position=j,
                finding=finding,
            )
    return sources


def build_input(plan: str, experts: list[ExpertInput]) -> str:
    """The synthesis call's user content: plan, successful work with IDs, missing coverage.

    Failed experts contribute only their assignment and an "unavailable"
    marker — never their error text or any response content.
    """
    completed: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for expert in experts:
        assignment = {
            "expert": f"e{expert.position}",
            "perspective": expert.assignment.perspective.strip(),
            "objective": expert.assignment.objective,
            "questions": expert.assignment.questions,
        }
        if expert.report is None:
            unavailable.append({**assignment, "status": "unavailable"})
            continue
        findings = [
            {"id": f"e{expert.position}-f{j}", **finding.model_dump()}
            for j, finding in enumerate(expert.report.findings, start=1)
        ]
        completed.append({**assignment, "status": "completed", "findings": findings})

    def dump(value: object) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False)

    return (
        f"Plan:\n{plan}\n\n"
        f"Completed expert investigations, with candidate findings and their source IDs:\n"
        f"{dump(completed)}\n\n"
        "Failed expert investigations (unavailable; missing coverage, not evidence "
        f"that no concerns exist):\n{dump(unavailable)}"
    )


@dataclass(frozen=True)
class SynthesisRun:
    report: SynthesisReport
    model: str
    usage: UsageSummary


def _unknown_source_issues(
    report: SynthesisReport, sources: dict[str, SourceRef]
) -> tuple[ValidationIssue, ...]:
    return tuple(
        ValidationIssue(
            schema="synthesis_report",
            loc=f"findings.{i}.source_finding_ids.{j}",
            type="unknown_source_id",
        )
        for i, finding in enumerate(report.findings)
        for j, source_id in enumerate(finding.source_finding_ids)
        if source_id not in sources
    )


async def synthesize(plan: str, experts: list[ExpertInput]) -> SynthesisRun:
    """Make the one synthesis call and return a report whose citations all resolve.

    Raises `SynthesisError` for an API error, refusal, truncation, missing
    text, output failing `SynthesisReport`, or an unknown source ID. Never
    retries or repairs.
    """
    api_key = get_anthropic_api_key()
    model = get_anthropic_model()
    sources = source_map(experts)
    usage = UsageSummary()

    async with anthropic.AsyncAnthropic(
        api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
    ) as client:
        try:
            response = await _call(
                client,
                usage,
                model=model,
                max_tokens=SYNTHESIS_MAX_OUTPUT_TOKENS,
                system=SYNTHESIS_SYSTEM_PROMPT,
                user_content=build_input(plan, experts),
                output_schema=SynthesisReport,
                stage="synthesis",
            )
        except ExpertError as exc:
            raise SynthesisError(
                f"Synthesis failed: {exc}", exc.usage, diagnostics=exc.diagnostics
            ) from exc

    def failure(message: str, issues: tuple[ValidationIssue, ...] | None = None) -> SynthesisError:
        return SynthesisError(
            message,
            usage,
            returned_model=response.model,
            diagnostics=FailureDiagnostics(
                failure_stage="synthesis",
                stop_reason=response.stop_reason,
                validation_errors=issues,
            ),
        )

    try:
        _check_stop_reason(response, usage, "synthesis", prefix="Synthesis failed: ")
    except ExpertError as exc:
        raise SynthesisError(
            str(exc), exc.usage, returned_model=response.model, diagnostics=exc.diagnostics
        ) from exc

    text = _response_text(response)
    if text is None:
        raise failure("Synthesis returned no output.")
    try:
        report = SynthesisReport.model_validate_json(text)
    except pydantic.ValidationError as exc:
        details = _describe_validation_error(exc)
        issues = _validation_issues("synthesis_report", exc)
        raise failure(f"Synthesis output was invalid ({details})", issues) from exc

    unknown = _unknown_source_issues(report, sources)
    if unknown:
        # The unknown IDs themselves are model output, so only their positions are reported.
        raise failure(f"Synthesis cited {len(unknown)} unknown source finding ID(s)", unknown)

    return SynthesisRun(report=report, model=response.model, usage=usage)
