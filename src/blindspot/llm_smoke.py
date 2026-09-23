"""Command-line smoke check for the Anthropic API connection.

Run with: uv run python -m blindspot.llm_smoke

Makes one billable request. Requires ANTHROPIC_API_KEY (see
blindspot.config.get_anthropic_api_key) and ANTHROPIC_MODEL to be set.
"""

import asyncio
import os
import sys

import anthropic

from blindspot.config import get_anthropic_api_key

PROMPT = "Reply with exactly: Blindspot connection OK."
MAX_OUTPUT_TOKENS = 64
REQUEST_TIMEOUT_SECONDS = 30.0


def _get_model() -> str:
    model = os.environ.get("ANTHROPIC_MODEL")
    if model is None or not model.strip():
        raise ValueError(
            "ANTHROPIC_MODEL is not set. Set it in .env or your shell to the "
            "Anthropic model ID you want to use."
        )
    return model


async def _run() -> int:
    try:
        api_key = get_anthropic_api_key()
        model = _get_model()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    async with anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=0,
    ) as client:
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                messages=[{"role": "user", "content": PROMPT}],
            )
        except anthropic.AuthenticationError as exc:
            print(f"Authentication error: {exc.message}", file=sys.stderr)
            return 1
        except anthropic.RateLimitError as exc:
            print(f"Rate-limit error: {exc.message}", file=sys.stderr)
            return 1
        except anthropic.APIConnectionError as exc:
            print(f"Connection error: {exc}", file=sys.stderr)
            return 1
        except anthropic.APIStatusError as exc:
            print(f"API error ({exc.status_code}): {exc.message}", file=sys.stderr)
            return 1

    text = "".join(block.text for block in response.content if block.type == "text")
    print(f"Response text: {text}")
    print(f"Model: {response.model}")
    print(f"Input tokens: {response.usage.input_tokens}")
    print(f"Output tokens: {response.usage.output_tokens}")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
