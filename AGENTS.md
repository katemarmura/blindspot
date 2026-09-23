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

## Current scope: minimal runnable server, no domain logic

**As of now, this repository contains a project scaffold plus a bare FastAPI server** — a
Python package, tooling, CI, and one `GET /health` endpoint. There is no orchestrator, no
expert agents, no pipeline, and no domain contracts (`Finding`, `ExpertDefinition`,
pipeline state, etc.) yet. Those are deliberately being designed before they are
implemented (see the open questions in `docs/project-brief.md`).

There is one standalone diagnostic script, `src/blindspot/llm_smoke.py`, that makes a
real Anthropic API call to verify credentials/connectivity — it is not part of the
application and nothing else imports it.

Do not infer architecture from this file beyond what's stated. Any structure diagrams or
role lists in `docs/project-brief.md` are explicitly **provisional** — starting points for
discussion, not specifications to implement as-is.

## Tech stack

- **Language:** Python 3.13 (see `.python-version`)
- **Package/env manager:** [uv](https://docs.astral.sh/uv/) — `pyproject.toml` +
  `uv.lock` (committed, reproducible)
- **Web framework:** FastAPI, served by Uvicorn (`src/blindspot/main.py`)
- **Config:** `src/blindspot/config.py` — `get_anthropic_api_key()` lazily loads
  `ANTHROPIC_API_KEY` from `.env` (via `python-dotenv`) or the shell environment. Only
  `llm_smoke.py` calls it; the FastAPI app doesn't, and doesn't need a key to run.
- **LLM smoke check:** `src/blindspot/llm_smoke.py` (`uv run python -m blindspot.llm_smoke`)
  — the official `anthropic` SDK, async client, one request. Verifies `ANTHROPIC_API_KEY` +
  `ANTHROPIC_MODEL` are configured and reachable. Not an LLM integration for the product;
  don't build on top of it without deciding that deliberately.
- **Lint/format:** Ruff
- **Type checker:** mypy (`strict` mode)
- **Tests:** pytest, with FastAPI's `TestClient` (needs `httpx2`, a dev dependency) for
  endpoint tests
- **Layout:** `src/` layout — the package is `src/blindspot/`, importable only once
  installed (via `uv sync`), which avoids accidentally importing from the working
  directory instead of the installed package.

Likely future additions (not yet present, do not add speculatively): Pydantic domain
models, an async LLM client that actually calls Anthropic, and eventually persistent
pipeline state. Do not introduce an agent framework, a database, Docker, or a frontend
until the domain contracts and first pipeline are designed.

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
