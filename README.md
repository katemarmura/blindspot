# Blindspot

Blindspot answers one question: **"What am I missing?"**

You describe a plan, decision, or idea; a system of independent agents examines it from
multiple perspectives and surfaces a small number of useful, non-obvious blind spots —
hidden assumptions, dependencies, risks, and questions you haven't thought to ask.

This is a learning project for multi-agent systems, orchestration, and Python backend
development. See [`docs/project-brief.md`](docs/project-brief.md) for the full design
context and [`AGENTS.md`](AGENTS.md) for the current project scope.

**Status:** minimal runnable FastAPI server with a health check. No domain logic, agents,
or pipeline yet.

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
IDs). Run project commands from the repository root, so `.env` is found. This only adds
configuration *support*, not a real integration: `get_anthropic_api_key()`
(`src/blindspot/config.py`) exists but is not called by the server, and `blindspot.llm_smoke`
is a standalone CLI check, not a dependency of the app. The server and its endpoints work
without either variable set.

#### Anthropic connection smoke check

```bash
uv run python -m blindspot.llm_smoke
```

Sends one short prompt to the configured model and prints the reply, the model ID that
responded, and the input/output token counts. **This makes a real, billable API request.**
Requires `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` to be set first.

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
src/blindspot/       # the blindspot package (src layout: importable only after install)
src/blindspot/main.py  # FastAPI app (currently just GET /health)
tests/                # pytest suite
docs/                 # design notes and the original project brief
```
