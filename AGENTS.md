# AGENTS.md

Instructions for coding agents (and humans) working in this repository.

## What this project is

Blindspot is a pet project exploring multi-agent systems, agent orchestration, dynamic
agent selection, skills/tools, structured outputs, shared state, agent-to-agent review,
prompt design, model routing, and evaluation of agent pipelines — with a real Python
backend, not a toy demo.

The product idea: a user describes a plan, decision, or intention. Blindspot analyzes it
from multiple independent perspectives to surface a small number of useful, non-obvious
blind spots (hidden assumptions, dependencies, risks, second-order effects, contradictions)
rather than an exhaustive list of generic risks.

Full context, motivation, and design exploration live in
[`docs/project-brief.md`](docs/project-brief.md). Treat that file as the source of truth
for intent; treat this file as the quick orientation for how to work in the repo day to day.

## Current scope: minimal server, a single-expert experiment, a multi-expert runner (manual or auto-selected team), and a model-comparison CLI

**As of now, this repository contains a project scaffold, a bare FastAPI server, and one
small domain contract with one real LLM call (plus at most one bounded recovery call)
built on it. It runs alone, concurrently for 1–3 caller-supplied perspectives, for a
1–3 expert team proposed by one team-selector call, or across two named models for manual
comparison.**
There is still no synthesis or cross-review, no adaptive rounds, no skills, tools,
permissions or model routing, no shared investigation state, and no database. Those are
deliberately being designed before they are implemented (see the open questions in
`docs/project-brief.md`).

What exists:
- `src/blindspot/schemas.py` — `Finding` and `ExpertReport` (Pydantic, `extra="forbid"`,
  nonblank strings, 0–3 findings), plus the team contract: `ExpertDefinition`
  (`perspective`, `objective`, 1–3 `questions`; all nonblank) and `TeamPlan` (1–3 experts,
  perspectives unique after trimming and ignoring case). All reject unexpected fields.
  Intentionally small: no IDs, scores, categories, evidence, review state, skills,
  permissions, or model routing.
- `src/blindspot/prompts.py` — `EXPERT_SYSTEM_PROMPT` (the expert; includes one paragraph on
  using a supplied objective and questions), `SELECTION_SYSTEM_PROMPT` (the bounded recovery
  step below), and `TEAM_SELECTOR_SYSTEM_PROMPT` (team selection).
- `src/blindspot/expert.py` — `run_expert(plan, perspective)`, one async call to one
  model using structured output (`messages.create` with
  `output_config={"format": {"type": "json_schema", "schema": transform_schema(ExpertReport)}}`
  — the same request `messages.parse(output_format=ExpertReport)` sends), validated
  locally against `ExpertReport` so the response and its usage survive a validation
  failure. Also a CLI:
  `uv run python -m blindspot.expert --plan "..." --perspective "..."`.
- **Bounded recovery (one possible second billable call).** The API only sees
  `maxItems: 3` as a description hint, so the model can return more than three findings.
  If a normally completed (`end_turn`) response has >3 findings that each satisfy
  `Finding` and no other schema violation (checked with the private, unbounded
  `_CandidateReport`), exactly one extra call to the same model asks for up to three
  zero-based indices (`_Selection`: strict ints, unique, 0–3, in bounds; 256 max output
  tokens, same 60s timeout, `max_retries=0`). The final `ExpertReport` is built in Python
  from the original `Finding` objects — wording is never model-rewritten. Refusals,
  truncation, API errors, malformed JSON, and invalid individual findings are never
  recovered; a failed selection is an error, never a third call. `ExpertRun.usage` /
  `ExpertError.usage` (`UsageSummary`) carry call count, whether recovery was attempted,
  and tokens summed across calls; calls that returned no usage are counted separately
  so totals are marked incomplete rather than treated as zero. `ExpertError.returned_model`
  holds the initial response's model ID for any failure after a response arrived
  (truncation, refusal, invalid output, failed recovery); it is `None` only when no response
  was received. `ExpertError.diagnostics` (`FailureDiagnostics`) records `failure_stage`,
  the failing response's `stop_reason` (None if no response arrived), `recovery_skip_reason`
  (initial stage only), and `validation_errors` as `ValidationIssue(schema, loc, type)`.
  These come from the failure itself, never by parsing messages. For initial validation
  failures, `_CandidateReport` errors are included alongside `ExpertReport`'s, because the
  bounded schema reports only `too_long` and hides per-finding errors. Locations pass
  through `_sanitize_loc`: names outside our schemas become `<unknown field>`, in both
  diagnostics and messages. `compare` and `pipeline` save `diagnostics` per failed run
  (`null` on success). This is a single fixed
  recovery step — not generic retry infrastructure; don't generalize it.
- `src/blindspot/pipeline.py` — `run_pipeline(plan, perspectives)`: validates the plan,
  1–3 perspectives (trimmed, nonblank, case-insensitively unique), and config before any
  call, then runs `run_expert` once per perspective concurrently in an
  `asyncio.TaskGroup`. Each expert gets only the plan and its own perspective. Returns a
  `PipelineResult` of `ExpertOutcome`s in input order (success with `ExpertRun`, or a
  sanitized `ExpertError` message plus whatever `UsageSummary` and `returned_model` it
  carried; monotonic
  elapsed time) and an overall `success`/`partial`/`failed` status. Only `ExpertError` is
  captured per expert; any other exception cancels the siblings and propagates. No
  pipeline-level retries; findings are never merged, ranked, or deduplicated. Up to 3
  experts × (1 call + ≤1 recovery) = **at most six billable requests**. CLI:
  `uv run python -m blindspot.pipeline --plan "..." --perspective "..." [--perspective ...]`
  (JSON to stdout, usage summary + elapsed time to stderr, exit 0 only if all succeed).
- `src/blindspot/team.py` — `PERSPECTIVE_CATALOGUE` (11 broad names; selection options,
  not agent classes; custom perspectives allowed) and `select_team(plan)`. It makes one
  structured-output call to the configured model: 60s timeout, 1500 max output tokens,
  `max_retries=0`, no repair. The catalogue goes in the user message, not the system prompt.
  Output is validated locally against `TeamPlan`. An API error, refusal, truncation, missing
  text, or invalid team raises `TeamSelectionError`, an `ExpertError` subclass with the same
  usage, `returned_model`, and `FailureDiagnostics` (`failure_stage="team_selection"`,
  `validation_errors` with `schema="team_plan"`). It reuses `expert.py`'s private
  `_call`/`_check_stop_reason`/validation helpers.
- `pipeline.run_auto_team(plan)` (CLI `--auto-team`, mutually exclusive with
  `--perspective`):
  - It validates the plan and config, creates `runs/team-<UTC>-<hex>/`, then calls
    `select_team`.
  - It writes `team.json` (plan, git state, selector settings, team or selection failure
    with diagnostics and usage) **before any expert starts**. A failed selection starts no
    experts.
  - Otherwise `run_team` runs the team's experts concurrently with the same runner as
    manual mode. Each expert gets the plan plus only its own perspective, objective, and
    questions.
  - It then writes `result.json`. CLI JSON adds `team`, `team_selection`, per-expert
    `assignment`, and `total_usage` (selector plus experts; each expert's usage already
    includes its recovery, so nothing is double-counted). The team is printed to stderr
    before the experts run.
  - **At most seven billable requests**: 1 selector + 3 × (1 + ≤1 recovery).
- `run_expert(..., *, objective=None, questions=None)`: when given, these are appended to
  the expert's input as labelled `Objective:` / `Investigation questions:` sections.
  Omitted, the input is exactly `Plan:`/`Perspective:` as before (manual runs, compare).
  The finding-selection recovery call receives that same input, assignment included, followed
  by `Candidate findings:`, so selection judges candidates against the same objective.
- `run_expert(..., *, model=None)`: an optional keyword-only override, used for both the
  initial and recovery calls. When given, `ANTHROPIC_MODEL` isn't needed. The override never
  mutates the environment. Omitted, the configured model is used as before.
- `src/blindspot/compare.py`: a model-comparison CLI,
  `uv run python -m blindspot.compare --plan ... --perspective ... --model A --model B [--repeats 1-3]`.
  - It validates all arguments and the API key before creating anything.
  - It runs `run_expert(model=...)` sequentially, alternating which model goes first each
    repetition, with no comparison-level retries.
  - It writes `runs/<id>/manifest.json` (plan, perspective, models, repeats, UTC time,
    exact prompts, `request_output_schemas()`, limits, git revision and dirty flag or
    `null`), then one `run-*.json` per run as soon as it completes. Files are written via a
    temp file and rename, so an interruption never leaves a truncated file.
  - Only `ExpertError` is recorded as a failure; bugs and cancellation propagate, and
    earlier files are kept.
  - It prints a per-model summary (successes/failures, recoveries, known tokens marked
    `(incomplete)`, average elapsed) and exits nonzero if any run failed.
  - Saved files contain the user's plan and the generated findings; `runs/` is git-ignored.
  - At most 2 requests per run: 4 for one repetition, 12 for three.
  - There is no quality scoring, no cost calculation, and no LLM judge. Small comparisons
    are exploratory, not a model ranking.
- `src/blindspot/runs.py` — shared `runs/` helpers (`create_run_dir`, atomic `write_json`,
  `utc_now`, `git_state`) used by `compare` and `--auto-team`.
- `src/blindspot/llm_smoke.py` — the pre-existing connectivity-only diagnostic
  (`uv run python -m blindspot.llm_smoke`), unaffected by the above; still not part of
  the application.

The runner is plain concurrent fan-out: over perspectives the caller names, or over one
team proposed by a single selector call. Do not read adaptive orchestration, synthesis,
cross-review, or any of the `docs/project-brief.md` pipeline diagrams into it.

Do not infer architecture from this file beyond what's stated. Any structure diagrams or
role lists in `docs/project-brief.md` are explicitly **provisional** — starting points for
discussion, not specifications to implement as-is.

## Tech stack

- **Language:** Python 3.13 (see `.python-version`)
- **Package/env manager:** [uv](https://docs.astral.sh/uv/) — `pyproject.toml` +
  `uv.lock` (committed, reproducible)
- **Web framework:** FastAPI, served by Uvicorn (`src/blindspot/main.py`)
- **Config:** `src/blindspot/config.py` — `get_anthropic_api_key()` and
  `get_anthropic_model()` lazily load `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` from `.env`
  (via `python-dotenv`) or the shell environment, no hardcoded model fallback. Only
  `llm_smoke.py`, `expert.py`, `team.py`, and `pipeline.py`/`compare.py` (up-front checks) call these; the FastAPI app doesn't, and doesn't need
  either to run.
- **Domain schema:** `src/blindspot/schemas.py` — `Finding` / `ExpertReport` and
  `ExpertDefinition` / `TeamPlan` (Pydantic); see the scope note above for what they
  deliberately exclude.
- **Expert call:** `src/blindspot/expert.py` (`uv run python -m blindspot.expert --plan
  ... --perspective ...`) — one async Anthropic structured-output call per invocation
  (`messages.create` + `output_config`, validated locally against `ExpertReport`), 60s
  timeout, `max_retries=0`, 3000 max output tokens; plus at most one 256-token
  finding-selection call when the model returns more than three valid findings (see
  above). Not a reusable "run an expert" abstraction.
- **LLM smoke check:** `src/blindspot/llm_smoke.py` (`uv run python -m blindspot.llm_smoke`)
  — the official `anthropic` SDK, async client, one request, no structured output.
  Verifies `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL` are configured and reachable only;
  unrelated to `expert.py` beyond sharing `config.py`.
- **Lint/format:** Ruff. `src/blindspot/prompts.py` is exempted from the E501
  line-length rule (`tool.ruff.lint.per-file-ignores`) — prompt text is stored verbatim,
  one line per source paragraph/bullet; don't reflow it to satisfy the linter.
- **Type checker:** mypy (`strict` mode)
- **Tests:** pytest — no `pytest-asyncio`; async functions are tested by wrapping calls
  in `asyncio.run(...)` inside plain `def test_...()` functions (see `tests/test_expert.py`).
  FastAPI's `TestClient` (needs `httpx2`, a dev dependency) for endpoint tests. Anthropic
  SDK calls are tested against a hand-written fake client/`messages` object, never the
  real network. `tests/test_pipeline.py` replaces `pipeline.run_expert` with a fake and
  uses `asyncio.Event`s (not timing thresholds) to prove overlap and completion order.
  `tests/test_compare.py` replaces `compare.run_expert` the same way and works in a
  `tmp_path` cwd, so `runs/` is created there. One end-to-end test goes through the real
  `run_expert` with a fake Anthropic client. `tests/test_team.py` covers the team schema
  and `select_team` against a fake client; `tests/test_auto_team.py` replaces
  `pipeline.select_team` and `pipeline.run_expert`, plus one end-to-end test through both
  with a fake client.
- **Layout:** `src/` layout — the package is `src/blindspot/`, importable only once
  installed (via `uv sync`), which avoids accidentally importing from the working
  directory instead of the installed package.

Likely future additions (not yet present, do not add speculatively): synthesis,
cross-review, adaptive rounds, skills/tools/permissions, model routing, and shared
investigation state. Do not introduce an agent framework, a database, Docker, or a frontend until the
rest of the domain contracts and first pipeline are designed.

## Development commands

```bash
uv sync                                          # install/update the environment from uv.lock
uv run uvicorn blindspot.main:app --reload       # run the dev server
uv run pytest                                    # run tests
uv run ruff check .                              # lint
uv run ruff format .                             # format
uv run mypy                                      # type-check
```

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `mypy`, and
`pytest` on every push and pull request. Run the same commands locally before pushing.

## Conventions

- Keep `pyproject.toml` and `uv.lock` in sync — run `uv lock` after manual edits to
  `pyproject.toml`, and commit the lockfile.
- New runtime dependencies: `uv add <package>`. Dev-only tooling: `uv add --dev <package>`.
- The user maintaining this project is an experienced TypeScript/JavaScript engineer
  currently learning Python — when behavior or conventions differ from typical JS/TS
  tooling in a non-obvious way, a short note is welcome, but don't over-explain
  well-known Python basics.
