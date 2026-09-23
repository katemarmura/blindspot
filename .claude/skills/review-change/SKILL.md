---
name: review-change
description: Review a Blindspot implementation against its requested scope. Use when asked to review a change, verify completed work, or check an implementation before proceeding.
---

# Review a Blindspot change

Review the implementation and report findings. Do not modify project files unless the user also asks for fixes.

## Establish scope

- Read AGENTS.md for current conventions and project scope.
- Identify the requested behavior from the user's task or supplied specification.
- Inspect Git status and the relevant diff, including untracked files.
- Use an explicitly supplied commit, branch, or PR when provided. Otherwise use conversation context to identify the change; ask only if the review target remains ambiguous.
- State which change you reviewed.
- Treat proposals in docs/project-brief.md as provisional unless the user has adopted them.

## Inspect the implementation

Read changed files and enough surrounding code to assess:

- Correctness and missing requirements.
- Regressions and relevant error handling.
- Whether tests exercise the requested behavior.
- Dependency, lockfile, and configuration consistency.
- Whether documentation matches the actual implementation.
- Unrequested features or abstractions that materially expand the task.

Keep findings proportional to the change. Do not request future agents, persistence, frameworks, or production infrastructure merely because the project may eventually need them.

Verify unfamiliar dependency behavior against installed source or official documentation before declaring it incorrect.

## Verify

Use the project's documented commands. For Python changes, normally run:

- uv run ruff check .
- uv run ruff format --check .
- uv run mypy
- uv run pytest

When dependencies change, also check lockfile consistency with:
- uv lock --check

For documentation-only changes, inspect content and references; do not run unrelated Python checks.

Do not run auto-formatting or auto-fix commands during review. Avoid commands that rewrite dependencies or lockfiles; use locked execution where appropriate.

Do not claim remote CI passed based on local checks. If a check cannot run, explain the limitation and continue the remaining review.

## Report

Lead with actionable findings, ordered by importance. For each finding, provide:

- The affected file and line.
- The concrete problem and the condition that triggers it.
- Its consequence.
- A concise suggested correction.

Separate minor documentation issues or optional suggestions from defects. Avoid style preferences already handled by tooling.

If no actionable defects are found, say so directly.

Finish with a short verification summary:
- Scope reviewed.
- Checks passed, failed, or not run.
- Any material uncertainty.

Keep the response concise. Do not invent findings to fill a quota.
