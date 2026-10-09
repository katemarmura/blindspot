"""Saving experiment results under the git-ignored `runs/` directory.

Shared by `blindspot.compare` and `blindspot.pipeline --auto-team`. Paths are
relative to the current working directory (run from the repository root).
"""

import json
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNS_DIR = Path("runs")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def git_state() -> dict[str, Any]:
    """The current git revision and dirty flag, or nulls when unavailable."""
    revision = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain") if revision is not None else None
    return {
        "revision": revision.strip() if revision is not None else None,
        "dirty": bool(status.strip()) if status is not None else None,
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    # Write to a temporary file then rename, so an interruption never leaves
    # a truncated result file behind. The temporary file is new and uniquely
    # named (O_EXCL), so a pre-existing file or symlink is never written through.
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def create_run_dir(prefix: str = "", runs_dir: Path = RUNS_DIR) -> tuple[str, Path]:
    """Create a new, uniquely named directory under `runs_dir`; return (id, path)."""
    run_id = f"{prefix}{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    directory = runs_dir / run_id
    directory.mkdir(parents=True, exist_ok=False)
    return run_id, directory
