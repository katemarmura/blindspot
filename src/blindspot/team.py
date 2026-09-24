"""Automatic expert-team selection: one structured-output call, no repair.

The selector proposes one to three `ExpertDefinition`s for a plan, drawing on
a predefined catalogue of broad perspectives (or custom ones). Its output is
validated locally against `TeamPlan`; a refusal, truncation, or invalid team
raises `TeamSelectionError` and no expert should start.
"""

from dataclasses import dataclass

import anthropic
import pydantic

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.expert import (
    REQUEST_TIMEOUT_SECONDS,
    ExpertError,
    FailureDiagnostics,
    UsageSummary,
    _call,
    _check_stop_reason,
    _describe_validation_error,
    _response_text,
    _validation_issues,
)
from blindspot.prompts import TEAM_SELECTOR_SYSTEM_PROMPT
from blindspot.schemas import TeamPlan

TEAM_SELECTOR_MAX_OUTPUT_TOKENS = 1500

# Broad selection options, not agent classes. The selector may also create
# custom perspectives.
PERSPECTIVE_CATALOGUE: tuple[str, ...] = (
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


class TeamSelectionError(ExpertError):
    """The team-selector call failed; carries the same usage/model/diagnostics as `ExpertError`."""


@dataclass(frozen=True)
class TeamSelection:
    team: TeamPlan
    model: str
    usage: UsageSummary


def _user_content(plan: str) -> str:
    catalogue = "\n".join(f"- {name}" for name in PERSPECTIVE_CATALOGUE)
    return f"Perspective catalogue:\n{catalogue}\n\nPlan:\n{plan}"


def _failure(
    message: str,
    usage: UsageSummary,
    stop_reason: str | None,
    exc: pydantic.ValidationError | None = None,
) -> TeamSelectionError:
    return TeamSelectionError(
        message,
        usage,
        diagnostics=FailureDiagnostics(
            failure_stage="team_selection",
            stop_reason=stop_reason,
            validation_errors=_validation_issues("team_plan", exc) if exc is not None else None,
        ),
    )


async def select_team(plan: str) -> TeamSelection:
    """Propose an expert team for `plan` with one call to the configured model.

    Raises `ValueError` for a blank plan or missing configuration, or
    `TeamSelectionError` for an API error, refusal, truncation, missing text,
    or output that fails `TeamPlan` validation. Never retries or repairs.
    """
    if not plan.strip():
        raise ValueError("plan must not be blank")
    api_key = get_anthropic_api_key()
    model = get_anthropic_model()
    usage = UsageSummary()

    async with anthropic.AsyncAnthropic(
        api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
    ) as client:
        try:
            response = await _call(
                client,
                usage,
                model=model,
                max_tokens=TEAM_SELECTOR_MAX_OUTPUT_TOKENS,
                system=TEAM_SELECTOR_SYSTEM_PROMPT,
                user_content=_user_content(plan),
                output_schema=TeamPlan,
                stage="team_selection",
            )
        except ExpertError as exc:
            raise TeamSelectionError(
                f"Team selection failed: {exc}", exc.usage, diagnostics=exc.diagnostics
            ) from exc

    try:
        _check_stop_reason(response, usage, "team_selection", prefix="Team selection failed: ")
        text = _response_text(response)
        if text is None:
            raise _failure("Team selection returned no output.", usage, response.stop_reason)
        try:
            team = TeamPlan.model_validate_json(text)
        except pydantic.ValidationError as exc:
            details = _describe_validation_error(exc)
            raise _failure(
                f"Team selection output was invalid ({details})", usage, response.stop_reason, exc
            ) from exc
    except ExpertError as exc:
        raise TeamSelectionError(
            str(exc), exc.usage, returned_model=response.model, diagnostics=exc.diagnostics
        ) from exc

    return TeamSelection(team=team, model=response.model, usage=usage)
