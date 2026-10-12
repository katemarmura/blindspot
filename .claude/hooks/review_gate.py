"""Claude Code hook: remind about the develop workflow and gate unreviewed changes.

One script handles four hook events (configured in `.claude/settings.json`):

- UserPromptSubmit: records a fingerprint of the working tree's content, resets the
  block counter, and adds a one-line reminder to use the `develop` workflow for
  implementation requests. A prompt containing an explicit skip phrase (SKIP_PATTERN,
  for example "skip review" or "pause the workflow") exempts that turn.
- SubagentStart / SubagentStop for `blindspot-reviewer`: a reviewer that finishes
  with the tree unchanged since it started marks that tree state as reviewed.
- Stop: if the tree changed since the prompt and that state hasn't been reviewed
  (and no reviewer is still running), it blocks the stop with a reason. A stop that
  follows a block with the tree unchanged since that block (the session declined
  to review) is allowed with a warning; edits after a block are blocked again.

The fingerprint covers file content only (tracked plus non-ignored untracked files),
not HEAD, so committing, stashing and re-applying, or checking out reviewed content
doesn't make it look unreviewed.

It can never block forever: at most MAX_BLOCKS blocks per user prompt, after which
it lets the session stop and shows the user a warning. Any error, a non-git
directory, or BLINDSPOT_REVIEW_GATE=off also lets the session stop. State lives in
the git directory, so it is never tracked. stdlib only; no network.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REVIEWER = "blindspot-reviewer"
MAX_BLOCKS = 2
REVIEWER_STALE_SECONDS = 45 * 60
KEEP_REVIEWED = 20
# Explicit, imperative requests to skip the review or the workflow. Most phrases must
# start a clause (start of text or line, after punctuation, or after please/and/but/so/then/just)
# so that descriptions such as "where we skip review" or "we don't review" don't match.
_LEAD = r"(?:^|[.,;:!?\n]\s*|\b(?:please|and|but|so|then|just)\s+)"
_OBJECT = (
    r"(?:the\s+|a\s+|any\s+)?"
    r"(?:develop\s+(?:workflow|skill|loop)|review(?:er|s|ing)?|workflow|develop)\b"
)
_CLAUSE_END = r"(?=\s*(?:[.,;:!?)]|$|this\s+time\b|for\s+now\b|please\b))"
SKIP_PATTERN = re.compile(
    "|".join(
        [
            _LEAD + r"(?:skip|bypass|pause)\s+" + _OBJECT,
            _LEAD
            + r"(?:don['\u2019]?t|do\s+not)\s+(?:(?:run|use|start|do|need|bother\s+with)\s+)?"
            + _OBJECT,
            r"\bwithout\s+" + _OBJECT + _CLAUSE_END,
            r"\bno\s+(?:review|reviews|workflow)\b"
            + r"(?:\s+(?:needed|necessary|required|please)\b|"
            + _CLAUSE_END
            + ")",
        ]
    ),
    re.IGNORECASE,
)

REMINDER = (
    "Blindspot review gate: if this request asks for an implementation or bug fix, "
    "use the project `develop` skill. Questions, planning, review-only and read-only "
    "inspection don't start the implementation loop."
)


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, check=True, timeout=20
    ).stdout


def fingerprint(cwd: Path) -> str:
    """Git tree ID of the working tree's content: tracked plus non-ignored untracked files.

    It doesn't depend on HEAD or on what is staged, so committing, stashing and
    re-applying, or checking out the same content gives the same fingerprint. It builds
    the tree in a throwaway index (seeded from a copy of the real one, timestamp
    included, for speed); the real index and the working tree are never modified.
    `git add` may store blob objects in the object database, which `git gc` later
    removes if unreferenced.
    """
    root = Path(_git(cwd, "rev-parse", "--show-toplevel").decode().strip())
    index_path = _git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")
    real_index = Path(index_path.decode().strip())
    work = state_dir(root)
    work.mkdir(parents=True, exist_ok=True)
    temp_index = work / f"index.{os.getpid()}.tmp"
    env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}
    try:
        try:
            # copy2 keeps the index's timestamp, which git needs to detect files
            # rewritten (same size) right after the index was written ("racy git").
            shutil.copy2(real_index, temp_index)
        except FileNotFoundError:  # no index yet: start empty
            temp_index.unlink(missing_ok=True)
        _git(root, "add", "-A", "--", ".", env=env)
        return _git(root, "write-tree", env=env).decode().strip()
    finally:
        temp_index.unlink(missing_ok=True)
        Path(f"{temp_index}.lock").unlink(missing_ok=True)


def state_dir(cwd: Path) -> Path:
    git_dir = Path(_git(cwd, "rev-parse", "--absolute-git-dir").decode().strip())
    return git_dir / "claude-review-gate"


def state_path(cwd: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id) or "unknown"
    return state_dir(cwd) / f"{safe}.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def handle(event: dict[str, Any], state: dict[str, Any], cwd: Path, now: float) -> dict[str, Any]:
    """Update `state` in place for one hook event; return the JSON to print (may be empty)."""
    name = event.get("hook_event_name")
    running: dict[str, Any] = state.setdefault("reviewers_running", {})
    reviewed: list[str] = state.setdefault("reviewed", [])

    if name == "UserPromptSubmit":
        state["prompt_fingerprint"] = fingerprint(cwd)
        state["blocks"] = 0
        state.pop("blocked_fingerprint", None)
        state["skip"] = bool(SKIP_PATTERN.search(str(event.get("prompt", ""))))
        if state["skip"]:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": REMINDER,
            }
        }

    if name in ("SubagentStart", "SubagentStop") and event.get("agent_type") == REVIEWER:
        agent_id = str(event.get("agent_id", ""))
        if name == "SubagentStart":
            running[agent_id] = {"started": now, "fingerprint": fingerprint(cwd)}
            return {}
        started = running.pop(agent_id, None)
        current = fingerprint(cwd)
        # Only a tree that didn't change during the review counts as reviewed.
        if isinstance(started, dict) and started.get("fingerprint") == current:
            if current not in reviewed:
                reviewed.append(current)
            del reviewed[:-KEEP_REVIEWED]
        return {}

    if name != "Stop":
        return {}

    baseline = state.get("prompt_fingerprint")
    if state.get("skip") or baseline is None:
        return {}
    current = fingerprint(cwd)
    if current == baseline or current in reviewed:
        return {}
    for agent_id, info in list(running.items()):
        started_at = info.get("started", 0) if isinstance(info, dict) else 0
        if now - float(started_at) > REVIEWER_STALE_SECONDS:
            del running[agent_id]
    if running:
        return {}  # a review is still in progress; its result arrives later
    unreviewed = f"Review gate: files changed in this turn haven't been reviewed by {REVIEWER}."
    if current == state.get("blocked_fingerprint"):
        # Nothing changed since the last block: the session declined to review.
        return {"systemMessage": f"{unreviewed} The session ended without a review."}
    blocks = int(state.get("blocks", 0))
    if blocks >= MAX_BLOCKS:
        return {
            "systemMessage": f"{unreviewed} The gate stopped asking after {MAX_BLOCKS} reminders."
        }
    state["blocks"] = blocks + 1
    state["blocked_fingerprint"] = current
    return {
        "decision": "block",
        "reason": (
            f"Review gate ({blocks + 1}/{MAX_BLOCKS}): files changed since the user's prompt "
            f"and the current state hasn't been reviewed by {REVIEWER}. If these are "
            "implementation changes, continue the develop workflow and run the review now. "
            "If no review applies (for example, the user asked to skip it or the changes "
            "aren't part of an implementation task), say so briefly and stop without "
            "changing files; that stop is allowed and the user sees a warning. Further "
            f"edits without a review are blocked again, at most {MAX_BLOCKS} times per prompt."
        ),
    }


def main() -> int:
    if os.environ.get("BLINDSPOT_REVIEW_GATE", "").lower() == "off":
        return 0
    try:
        event = json.load(sys.stdin)
        cwd = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
        path = state_path(cwd, str(event.get("session_id", "")))
        state = load_state(path)
        output = handle(event, state, cwd, time.time())
        save_state(path, state)
    except Exception as exc:  # never block the session because of a gate failure
        print(f"review gate skipped: {type(exc).__name__}", file=sys.stderr)
        return 0
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
