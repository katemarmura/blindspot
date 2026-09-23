"""Blindspot's first single-expert experiment: one perspective, one model call.

Run with:
    uv run python -m blindspot.expert --plan "..." --perspective "..."

This is a standalone experiment, not orchestration — there is no
ExpertDefinition abstraction, no multi-expert pipeline, and no persistence.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass

import anthropic
import pydantic

from blindspot.config import get_anthropic_api_key, get_anthropic_model
from blindspot.prompts import EXPERT_SYSTEM_PROMPT
from blindspot.schemas import ExpertReport

REQUEST_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_TOKENS = 1500


class ExpertError(Exception):
    """Raised when an expert run fails: refusal, truncation, invalid output, or an API error."""


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
    input_tokens: int
    output_tokens: int


async def run_expert(plan: str, perspective: str) -> ExpertRun:
    """Run one expert perspective over one plan and return a validated result.

    An `ExpertReport` with zero findings is a successful result, not an
    error. Raises `ValueError` for a blank `plan`/`perspective`, or
    `ExpertError` for a refusal, truncated response, output that fails
    `ExpertReport` validation, or an Anthropic API error.
    """
    if not plan.strip():
        raise ValueError("plan must not be blank")
    if not perspective.strip():
        raise ValueError("perspective must not be blank")

    api_key = get_anthropic_api_key()
    model = get_anthropic_model()

    user_content = f"Plan:\n{plan}\n\nPerspective:\n{perspective}"

    async with anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    ) as client:
        try:
            response = await client.messages.parse(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=EXPERT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
                output_format=ExpertReport,
            )
        except pydantic.ValidationError as exc:
            details = _describe_validation_error(exc)
            raise ExpertError(
                f"Model output did not match the expected schema ({details})"
            ) from exc
        except anthropic.AuthenticationError as exc:
            raise ExpertError(f"Authentication error: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise ExpertError(f"Rate-limit error: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ExpertError(f"Connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ExpertError(f"API error ({exc.status_code}): {exc.message}") from exc

    if response.stop_reason == "refusal":
        raise ExpertError("The model declined to respond to this request.")
    if response.stop_reason == "max_tokens":
        raise ExpertError(
            "The model response was truncated before completion (max output tokens reached)."
        )

    report = response.parsed_output
    if report is None:
        raise ExpertError("The model did not return output matching the expected schema.")

    return ExpertRun(
        report=report,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blindspot.expert",
        description="Run a single Blindspot expert perspective against a plan.",
    )
    parser.add_argument("--plan", required=True, help="The plan or decision to analyze.")
    parser.add_argument("--perspective", required=True, help="The perspective to analyze it from.")
    return parser.parse_args(argv)


async def _run(plan: str, perspective: str) -> int:
    try:
        run = await run_expert(plan, perspective)
    except (ValueError, ExpertError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(run.report.model_dump_json(indent=2))
    print(f"Model: {run.model}", file=sys.stderr)
    print(f"Input tokens: {run.input_tokens}", file=sys.stderr)
    print(f"Output tokens: {run.output_tokens}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args.plan, args.perspective))


if __name__ == "__main__":
    sys.exit(main())
