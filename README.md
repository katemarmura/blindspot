# Blindspot

Blindspot answers one question: **"What am I missing?"**

You describe a plan, decision, or idea; a system of independent agents examines it from
multiple perspectives and surfaces a small number of useful, non-obvious blind spots —
hidden assumptions, dependencies, risks, and questions you haven't thought to ask.

This is a learning project for multi-agent systems, orchestration, and Python backend
development. See [`docs/project-brief.md`](docs/project-brief.md) for the full design
context and [`AGENTS.md`](AGENTS.md) for the current project scope.

**Status:** minimal runnable FastAPI server with a health check, plus a first
single-expert experiment (`blindspot.expert`) that makes one real, Pydantic-validated
LLM call. No orchestration, multi-expert pipeline, or persistence yet.

## Requirements

- Python 3.13 (managed automatically by uv via `.python-version`)
- [uv](https://docs.astral.sh/uv/) for dependency and environment management

## Setup

```bash
uv sync
```

This creates `.venv` and installs the project plus dev dependencies (ruff, pytest, mypy),
pinned exactly as recorded in `uv.lock`.

### Configuration

```bash
cp .env.example .env
```

Then set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` in `.env` (pick a model ID you have
access to — see [platform.claude.com/docs](https://platform.claude.com/docs) for current
IDs). Run project commands from the repository root, so `.env` is found. `get_anthropic_api_key()`
and `get_anthropic_model()` (`src/blindspot/config.py`) are only called by the two CLI
scripts below, never by the server — `GET /health` works without either variable set.

#### Anthropic connection smoke check

```bash
uv run python -m blindspot.llm_smoke
```

Sends one short prompt to the configured model and prints the reply, the model ID that
responded, and the input/output token counts. **This makes a real, billable API request.**
Requires `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` to be set first.

#### Single-expert experiment

```bash
uv run python -m blindspot.expert \
  --plan "I want to open a small coffee shop in Warsaw." \
  --perspective "Financial"
```

Sends the plan and perspective to the configured model with one fixed system prompt
(`src/blindspot/prompts.py`), asking for zero to three findings. The response is
validated against the `ExpertReport`/`Finding` Pydantic models (`src/blindspot/schemas.py`)
before anything is printed. Validated JSON goes to stdout; the model ID and input/output
token counts go to stderr. Any failure (refusal, truncated output, output that fails
validation, or an API error) prints a concise message to stderr and exits nonzero — an
empty findings list is a successful result. **This makes a real, billable API request.**
This is a single, standalone expert call — there is no orchestrator, no multi-expert
pipeline, and no persistence yet.

## Development commands

| Command | Purpose |
| --- | --- |
| `uv run uvicorn blindspot.main:app --reload` | Run the dev server (http://127.0.0.1:8000) |
| `uv run pytest` | Run the test suite |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format |
| `uv run mypy` | Type-check (`strict` mode) |
| `uv add <package>` | Add a runtime dependency |
| `uv add --dev <package>` | Add a dev-only dependency |
| `uv lock` | Refresh `uv.lock` after editing `pyproject.toml` by hand |

Health check: `GET /health` → `200 {"status": "ok"}`.

All checks run in CI on every push and pull request — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Project layout

```text
src/blindspot/           # the blindspot package (src layout: importable only after install)
src/blindspot/main.py    # FastAPI app (currently just GET /health)
src/blindspot/config.py  # ANTHROPIC_API_KEY / ANTHROPIC_MODEL loading
src/blindspot/schemas.py # Finding / ExpertReport Pydantic output schema
src/blindspot/prompts.py # expert system prompt
src/blindspot/expert.py  # single-expert async call + CLI
src/blindspot/llm_smoke.py  # connection smoke-check CLI
tests/                   # pytest suite
docs/                    # design notes and the original project brief
```
