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

## Current scope: minimal server, a single-expert experiment, and a minimal multi-expert runner

**As of now, this repository contains a project scaffold, a bare FastAPI server, and one
small domain contract with one real LLM call (plus at most one bounded recovery call)
built on it, run either alone or concurrently for 1–3 caller-supplied perspectives.**
There is still no LLM orchestrator, no `ExpertDefinition` abstraction, no dynamic team
selection, no synthesis or cross-review, no pipeline state, and no persistence. Those are deliberately being designed before they are
implemented (see the open questions in `docs/project-brief.md`).

What exists:
- `src/blindspot/schemas.py` — `Finding` and `ExpertReport` (Pydantic, `extra="forbid"`,
  nonblank strings, 0–3 findings). The **first** domain contract — intentionally small
  (no IDs, scores, categories, evidence, or review state).
- `src/blindspot/prompts.py` — `EXPERT_SYSTEM_PROMPT` (the expert) and
  `SELECTION_SYSTEM_PROMPT` (the bounded recovery step below).
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
  so totals are marked incomplete rather than treated as zero. This is a single fixed
  recovery step — not generic retry infrastructure; don't generalize it.
- `src/blindspot/pipeline.py` — `run_pipeline(plan, perspectives)`: validates the plan,
  1–3 perspectives (trimmed, nonblank, case-insensitively unique), and config before any
  call, then runs `run_expert` once per perspective concurrently in an
  `asyncio.TaskGroup`. Each expert gets only the plan and its own perspective. Returns a
  `PipelineResult` of `ExpertOutcome`s in input order (success with `ExpertRun`, or a
  sanitized `ExpertError` message plus whatever `UsageSummary` it carried; monotonic
  elapsed time) and an overall `success`/`partial`/`failed` status. Only `ExpertError` is
  captured per expert; any other exception cancels the siblings and propagates. No
  pipeline-level retries; findings are never merged, ranked, or deduplicated. Up to 3
  experts × (1 call + ≤1 recovery) = **at most six billable requests**. CLI:
  `uv run python -m blindspot.pipeline --plan "..." --perspective "..." [--perspective ...]`
  (JSON to stdout, usage summary + elapsed time to stderr, exit 0 only if all succeed).
- `src/blindspot/llm_smoke.py` — the pre-existing connectivity-only diagnostic
  (`uv run python -m blindspot.llm_smoke`), unaffected by the above; still not part of
  the application.

The runner is plain concurrent fan-out over perspectives the caller names. Do not read
LLM orchestration, dynamic expert selection, synthesis, cross-review, or any of the
`docs/project-brief.md` pipeline diagrams into it.

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
  `llm_smoke.py`, `expert.py`, and `pipeline.py` (an up-front check) call these; the FastAPI app doesn't, and doesn't need
  either to run.
- **Domain schema:** `src/blindspot/schemas.py` — `Finding` / `ExpertReport` (Pydantic).
  The first and only domain contract so far; see the scope note above for what it
  deliberately excludes.
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
- **Layout:** `src/` layout — the package is `src/blindspot/`, importable only once
  installed (via `uv sync`), which avoids accidentally importing from the working
  directory instead of the installed package.

Likely future additions (not yet present, do not add speculatively): an orchestrator,
`ExpertDefinition`, dynamic perspective selection, synthesis, cross-review, and persistent
pipeline state. Do not introduce an agent framework, a database, Docker, or a frontend until the
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
