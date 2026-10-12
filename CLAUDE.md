# Claude Code development workflow

@AGENTS.md

For the main Claude Code session: implementation and bug-fix requests use the project
`develop` skill (`.claude/skills/develop/SKILL.md`) by default. Coordinate the requested
change, independent review, and bounded fixes before reporting completion. The explicit
entry point is `/develop <task>`; `/develop review current changes` reviews existing work
without authorizing edits. Questions, planning, and read-only artifact inspection do not
start an implementation loop. Respect explicit requests to pause or skip the workflow.

These instructions are for the coordinator. A delegated reviewer, fixer, or designer
performs only its assigned stage and returns its result; it must not start another
workflow or delegate recursively.

The orchestration is instruction-driven. A light hook (`.claude/hooks/review_gate.py`,
registered in `.claude/settings.json`) adds a one-line `develop` reminder to each prompt
and sends the session back at most twice per prompt if files changed without a
completed `blindspot-reviewer` run; after that it lets the turn end with a warning. It
checks that a review ran, not its verdict, and isn't a CI gate. If a block reason
arrives for changes that need no review, say so briefly and stop without editing files:
that stop is allowed (the user sees a warning), while further edits are blocked again.
Never claim a stage ran without its returned evidence. If delegation or a required check
is unavailable, report the workflow as incomplete rather than silently self-approving.
