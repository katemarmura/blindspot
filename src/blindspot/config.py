"""Configuration loading. Values are read lazily, only when requested."""

import os
from pathlib import Path

from dotenv import load_dotenv

_ANTHROPIC_API_KEY_VAR = "ANTHROPIC_API_KEY"


def get_anthropic_api_key() -> str:
    """Return the Anthropic API key.

    Loads `.env` from the current working directory (run project commands
    from the repository root) without overriding variables already set in
    the environment, then reads `ANTHROPIC_API_KEY`. Raises `ValueError` if
    the key is missing, empty, or whitespace-only. Does not validate the
    key's format or make a network request.
    """
    load_dotenv(Path.cwd() / ".env", override=False)

    value = os.environ.get(_ANTHROPIC_API_KEY_VAR)
    if value is None or not value.strip():
        raise ValueError(
            f"{_ANTHROPIC_API_KEY_VAR} is not set. Copy .env.example to .env and set "
            f"{_ANTHROPIC_API_KEY_VAR}, or export it in your shell."
        )
    return value
