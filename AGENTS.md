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

## Planned product direction: guided clarification

The intended experience asks one focused question at a time, shows its originating
perspective(s), and supports an answer, “I don't know,” “not applicable,” or deferral.
After a small batch, reassessment uses accumulated answers to refine concerns and select
useful follow-ups, ending with a short tailored conclusion. See the dated update in
[`docs/project-brief.md`](docs/project-brief.md) for semantics and open design decisions.

Only a first, manual round is implemented: `blindspot.clarification` (below) generates one
batch of up to three questions from a saved auto-team run and reassesses once from a
user-edited JSON response file. Not yet implemented: one-question-at-a-time interaction,
suggested answers, follow-up batches, stopping rules, and a tailored final conclusion.
Preserve the existing CLI as a baseline; do not speculatively add a UI, database,
framework, or automatic multi-round loop. Keep user answers distinct from expert
assumptions, and retain links to original findings.

## Current scope: minimal server, a single-expert experiment, a multi-expert runner (manual or auto-selected team), a model-comparison CLI, and one manual clarification round

**As of now, this repository contains a project scaffold, a bare FastAPI server, and one
small domain contract with one real LLM call (plus at most one bounded recovery call)
built on it. It runs alone, concurrently for 1–3 caller-supplied perspectives, for a
1–3 expert team proposed by one team-selector call (followed by one synthesis call), or
across two named models for manual comparison. A saved auto-team run can then go through
one clarification round (one question call, one reassessment call).**
There is still no cross-review, no adaptive rounds, no skills, tools,
permissions or model routing, no shared investigation state, and no database. Those are
deliberately being designed before they are implemented (see the open questions in
`docs/project-brief.md`).

What exists:
- `src/blindspot/schemas.py` — `Finding` and `ExpertReport` (Pydantic, `extra="forbid"`,
  nonblank strings, 0–3 findings), plus the team contract: `ExpertDefinition`
  (`perspective`, `objective`, 1–3 `questions`; all nonblank) and `TeamPlan` (1–3 experts,
  perspectives unique after trimming and ignoring case), plus the synthesis contract:
  `SynthesizedFinding` (`Finding`'s fields plus a nonempty list of unique, nonblank
  `source_finding_ids`) and `SynthesisReport` (0–5 findings). All reject unexpected fields.
  Intentionally small: no scores, categories, evidence, review state, skills,
  permissions, or model routing.
- `src/blindspot/prompts.py` — `EXPERT_SYSTEM_PROMPT` (the expert; includes one paragraph on
  using a supplied objective and questions), `SELECTION_SYSTEM_PROMPT` (the bounded recovery
  step below), `TEAM_SELECTOR_SYSTEM_PROMPT` (team selection), and
  `SYNTHESIS_SYSTEM_PROMPT` (synthesis).
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
  - It writes `experts.json` (expert outcomes plus the `sources` ID mapping) **before
    synthesis starts**, then runs synthesis (below), then writes `result.json`. CLI JSON
    adds `team`, `team_selection`, per-expert `assignment`, `sources`, `synthesis`, and
    `total_usage`. The total is selector + experts (each expert's usage already includes its
    recovery) + synthesis, each counted once. The team is printed to stderr before the
    experts run.
  - The top-level `status` is still expert-based (`success`/`partial`/`failed`).
    `synthesis.status` is separate:
    - `completed`: the call succeeded.
    - `no_findings`: experts succeeded but reported nothing, so an empty report is returned
      with no call.
    - `unavailable`: team selection failed or every expert failed; no call.
    - `failed`: the call failed; expert reports are kept, and model, usage, and diagnostics
      are preserved.
    Exit 0 only if every expert succeeded **and** synthesis is `completed`/`no_findings`.
  - **At most eight billable requests**: 1 selector + 3 × (1 + ≤1 recovery) + 1 synthesis.
- `src/blindspot/synthesis.py` (auto-team only):
  - `source_map`/`build_input` deterministically give each successful expert's finding the
    ID `e<team position>-f<finding position>` (1-based; positions are kept even when
    earlier experts failed).
  - The input contains the plan, completed experts' assignments and ID-tagged findings
    (empty reports included), and failed experts' assignments marked `unavailable`. It
    never includes error text.
  - `synthesize` makes one structured-output call: 60s timeout, 3000 max output tokens,
    `max_retries=0`, no repair. Output is validated locally against `SynthesisReport`, and
    then every cited ID is checked against the source map. An unknown ID is a failure with
    an `unknown_source_id` issue at its position; the ID itself isn't echoed, since it's
    model output.
  - Failures raise `SynthesisError` (an `ExpertError` subclass, `failure_stage="synthesis"`).
  - Original expert reports are never modified.
  - **Valid IDs establish traceability, not proof that a rewritten finding faithfully
    represents its sources.**
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
- `src/blindspot/clarification.py` — one clarification round over a saved auto-team run,
  as two explicit subcommands. Each makes at most one call (60s, `max_retries=0`, no
  recovery or repair) and reports only its own usage, so the original run's calls are not
  counted again.
  - **Contracts (`schemas.py`):**
    - The model returns a `QuestionBatch` of 0–3 `ProposedQuestion`s. Each has a
      question, `why_it_matters`, and a nonempty list of unique `source_finding_ids`.
    - `ClarificationQuestion` adds an app-assigned `id` (`q1`…), set only after
      validation. Originating perspectives are derived from the cited findings
      (`question_perspectives`), not stored.
    - `ResponseEntry` has `question_id` and `status`: `answered` (answer text required),
      `dont_know`, `not_applicable` (optional explanation), `deferred`, or `null`
      (unanswered). Text is rejected with `dont_know`, `deferred`, or `null`, and a blank
      string counts as absent.
    - `ResponseFile` holds the `clarification_id` and the entries.
    - `ReassessmentReport` has 0–5 each of `remaining_considerations` (finding IDs and
      optional question IDs), `addressed_concerns` (finding IDs and at least one question
      ID), and `unresolved_uncertainties` (at least one finding or question ID).
  - **`prepare --run runs/team-*`:**
    - `load_run` validates `result.json` and re-derives source IDs, checking them against a
      saved `sources` mapping if there is one (older runs have none). A completed synthesis
      is included if its citations resolve.
    - With no successful findings, it stops with no call and creates nothing.
    - Otherwise it makes one call (1500 tokens) with `synthesis.build_input` plus the
      existing synthesis, then checks every cited ID in Python (`unknown_source_id`).
    - It writes a new `runs/team-*/clarify-<UTC>-<hex>/` with `clarification.json`
      (`ClarificationArtifact`: plan, expert assignments and reports, synthesis, sources,
      questions, call metadata; `status: "failed"` plus diagnostics on a failed call) and,
      on success, a `responses.json` template.
  - **`reassess --clarification … --responses …`:**
    - It validates both files: the matching `clarification_id`, no unknown or duplicate
      question IDs, and valid status/answer combinations.
    - If every question is unanswered or deferred, it makes no call and saves nothing.
    - Otherwise it makes one call (15000 tokens) with the original plan and findings, the
      questions with perspectives, the exact recorded responses, and the unanswered IDs.
    - It checks references in Python: `unknown_source_id` and `unknown_question_id`, and
      `question_not_answered` when an addressed concern cites a question whose response
      isn't `answered`/`not_applicable`.
    - It saves a new `reassessment-<UTC>-<hex>.json` next to the artifact, with the
      recorded responses, report or failure, model, usage, and diagnostics. The artifact
      and the response file are never modified.
  - The reassessment prompt updates the supplied concerns; it is not a fresh search. Its
    sections have distinct roles:
    - remaining considerations are open original concerns;
    - addressed concerns say what specifically improved (a learned fact is not enough);
    - unresolved uncertainties are a specific missing fact plus the decision it would change.

    Its stopping rules:
    - accept a concrete reported arrangement for the gap it covers;
    - don't replace addressed concerns with doubts about quality or the people involved;
    - missing detail alone doesn't keep a concern open;
    - report problems an answer reveals;
    - a source ID doesn't justify a stricter concern;
    - empty sections are valid.

    It also requires titles that keep their item's uncertainty and no point repeated across
    sections, and it forbids inferring unstated dates, timing, or layout and hardening
    conditional findings.

    The rules stay general, with no domain-specific wording. Judge prompt changes with the
    manual fixture in `evals/reassessment/` (its `CHECKLIST.md`), not with mocked tests.
  - Diagnostics use the `clarification_questions` and `reassessment` stages (errors are
    `ClarificationError`); invalid input files raise `ClarificationInputError`.
  - Call diagnostics (`CallDiagnostics`) are saved as `call_diagnostics`, on success and on
    failure, in `question_generation` and in each `reassessment-*.json`. They record:
    - the requested model and output-token limit, and the elapsed time (failed calls
      included);
    - whether a response arrived, and if so the returned model, stop reason, and
      content-block type counts;
    - the total characters across text blocks;
    - `parse_text_is_valid_json`, which checks JSON syntax only (schema validation is
      separate) and is `null` when there's no text;
    - this call's usage.
    With no response, the response fields are `null`. Only types and lengths are kept:
    never text, thinking content, signatures, or headers. Nothing is inferred from text
    length (for example hidden reasoning tokens). On failure the CLI prints a two-line
    call/response summary; successful output is unchanged.
- `src/blindspot/llm_smoke.py` — the pre-existing connectivity-only diagnostic
  (`uv run python -m blindspot.llm_smoke`), unaffected by the above; still not part of
  the application.

The runner is plain concurrent fan-out: over perspectives the caller names, or over one
team proposed by a single selector call and followed by a single synthesis call. Do not read
adaptive orchestration, cross-review, or any of the `docs/project-brief.md` pipeline
diagrams into it.

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
  `llm_smoke.py`, `expert.py`, `team.py`, `synthesis.py`, `clarification.py`, and
  `pipeline.py`/`compare.py` (up-front checks) call these; the FastAPI app doesn't, and doesn't need
  either to run.
- **Domain schema:** `src/blindspot/schemas.py` — `Finding` / `ExpertReport`,
  `ExpertDefinition` / `TeamPlan`, the synthesis contract, and the clarification contracts
  (Pydantic); see the scope note above for what they deliberately exclude.
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
  `pipeline.select_team`, `pipeline.run_expert`, and `pipeline.synthesize`, plus end-to-end
  tests with a fake client. `tests/test_synthesis.py` covers the synthesis schema, source
  IDs, input building, and `synthesize`. `tests/conftest.py` has an autouse fixture that
  replaces `anthropic.AsyncAnthropic` with a factory that fails, so a test that forgets a
  mock fails instead of reaching the network. Tests needing the SDK path install their
  own fake on top. `tests/test_clarification.py` writes a saved run into `tmp_path` and
  covers the contracts, loading, both subcommands, the no-call cases, reference checks, and
  that original files are unchanged (by hashing them before and after).
  `tests/test_reassessment_eval_fixture.py` only checks that the committed `evals/` fixture
  stays loadable through `load_artifact`/`load_responses`. It is not a quality test.
- **Layout:** `src/` layout — the package is `src/blindspot/`, importable only once
  installed (via `uv sync`), which avoids accidentally importing from the working
  directory instead of the installed package.

Likely future additions (not yet present, do not add speculatively):
cross-review, adaptive rounds, skills/tools/permissions, model routing, and shared
investigation state. Do not introduce an agent framework, a database, Docker, or a frontend until the
rest of the domain contracts and first pipeline are designed.

## Development commands

```bash
make verify                                      # all CI checks, locked, no fixes (see Makefile)
uv sync                                          # install/update the environment from uv.lock
uv run uvicorn blindspot.main:app --reload       # run the dev server
uv run pytest                                    # run tests
uv run ruff check .                              # lint
uv run ruff format .                             # format
uv run mypy                                      # type-check
```

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `mypy`, and
`pytest` on every push and pull request. Run `make verify` (the same four, via
`uv run --locked`) before pushing, and keep the Makefile aligned with CI if CI changes.

Project-level Claude Code skills live in `.claude/skills/`:
- `review-change` reviews an implementation against its requested scope.
- `inspect-run` does a read-only review of saved `runs/` artifacts: stage outcomes, usage,
  tracing, and quality observations. It has a stdlib helper, `summarize_run.py`.

These are development aids, not Blindspot runtime capabilities. If an artifact format
changes, update `inspect-run`'s format table and helper.

## Conventions

- Keep `pyproject.toml` and `uv.lock` in sync — run `uv lock` after manual edits to
  `pyproject.toml`, and commit the lockfile.
- New runtime dependencies: `uv add <package>`. Dev-only tooling: `uv add --dev <package>`.
- The user maintaining this project is an experienced TypeScript/JavaScript engineer
  currently learning Python — when behavior or conventions differ from typical JS/TS
  tooling in a non-obvious way, a short note is welcome, but don't over-explain
  well-known Python basics.
