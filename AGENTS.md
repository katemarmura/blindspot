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

## Current scope: minimal server plus a first single-expert experiment

**As of now, this repository contains a project scaffold, a bare FastAPI server, and one
small domain contract with one real LLM call built on it.** There is still no
orchestrator, no `ExpertDefinition` abstraction, no multi-expert pipeline, no pipeline
state, and no persistence. Those are deliberately being designed before they are
implemented (see the open questions in `docs/project-brief.md`).

What exists:
- `src/blindspot/schemas.py` — `Finding` and `ExpertReport` (Pydantic, `extra="forbid"`,
  nonblank strings, 0–3 findings). The **first** domain contract — intentionally small
  (no IDs, scores, categories, evidence, or review state).
- `src/blindspot/prompts.py` — `EXPERT_SYSTEM_PROMPT`, one fixed system prompt.
- `src/blindspot/expert.py` — `run_expert(plan, perspective)`, one async call to one
  model, validated against `ExpertReport` via the Anthropic SDK's structured-output
  `messages.parse(..., output_format=ExpertReport)`. Also a CLI:
  `uv run python -m blindspot.expert --plan "..." --perspective "..."`.
- `src/blindspot/llm_smoke.py` — the pre-existing connectivity-only diagnostic
  (`uv run python -m blindspot.llm_smoke`), unaffected by the above; still not part of
  the application.

This is one expert, called directly, once. Do not read orchestration, dynamic expert
selection, cross-review, or any of the `docs/project-brief.md` pipeline diagrams into it.

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
  `llm_smoke.py` and `expert.py` call these; the FastAPI app doesn't, and doesn't need
  either to run.
- **Domain schema:** `src/blindspot/schemas.py` — `Finding` / `ExpertReport` (Pydantic).
  The first and only domain contract so far; see the scope note above for what it
  deliberately excludes.
- **Expert call:** `src/blindspot/expert.py` (`uv run python -m blindspot.expert --plan
  ... --perspective ...`) — one async Anthropic call per invocation, using
  `messages.parse(output_format=ExpertReport)`, 60s timeout, `max_retries=0`, 1500 max
  output tokens. A single call, not a reusable "run an expert" abstraction.
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
  real network.
- **Layout:** `src/` layout — the package is `src/blindspot/`, importable only once
  installed (via `uv sync`), which avoids accidentally importing from the working
  directory instead of the installed package.

Likely future additions (not yet present, do not add speculatively): an orchestrator,
`ExpertDefinition`, multiple experts/perspectives, cross-review, and persistent pipeline
state. Do not introduce an agent framework, a database, Docker, or a frontend until the
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
