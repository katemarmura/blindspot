# Blindspot

Blindspot answers one question: **"What am I missing?"**

You describe a plan, decision, or idea; a system of independent agents examines it from
multiple perspectives and surfaces a small number of useful, non-obvious blind spots —
hidden assumptions, dependencies, risks, and questions you haven't thought to ask.

This is a learning project for multi-agent systems, orchestration, and Python backend
development. See [`docs/project-brief.md`](docs/project-brief.md) for the full design
context and [`AGENTS.md`](AGENTS.md) for the current project scope.

**Status:** minimal runnable FastAPI server with a health check, a first single-expert
experiment (`blindspot.expert`) that makes one real, Pydantic-validated LLM call (plus at
most one bounded recovery call — see below), and a minimal runner (`blindspot.pipeline`)
that runs one to three explicitly named perspectives concurrently, plus a small
model-comparison command (`blindspot.compare`). No LLM orchestration, synthesis,
cross-review, or database yet.

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
and `get_anthropic_model()` (`src/blindspot/config.py`) are only called by the CLI
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
before anything is printed. Validated JSON goes to stdout; the model ID, model-call count,
and input/output token counts go to stderr. Any failure (refusal, truncated output, output that fails
validation, or an API error) prints a concise message to stderr and exits nonzero — an
empty findings list is a successful result. **This makes a real, billable API request.**
This is a single, standalone expert call; to run several perspectives, see the
multi-expert runner below.

**Bounded recovery (possible second billable request).** The API does not enforce the
three-finding limit (the SDK passes it to the model only as a schema hint), so the model
occasionally returns more. When — and only when — a normally completed response contains
more than three findings that are *each* valid and the report has no other schema
problem, the CLI makes **exactly one** extra request to the same model (60s timeout, at
most 256 output tokens, no automatic retries). That request shows the model the candidates
labelled by index and asks it to return up to three indices; the final report is then
built in Python from the original findings, so their wording is never rewritten. Refusals,
truncation, API errors, malformed JSON, or an invalid individual finding are not
recovered. If the selection is invalid (duplicate, out-of-range, non-integer, or more than
three indices) or the second request fails, the run fails — there is never a third call.
stderr reports whether recovery was attempted, the number of model calls, and input/output
tokens summed across both calls; if a call returned no usage (e.g. an API error), the
totals are marked incomplete rather than counted as zero.

**Failure diagnostics.** On failure, stderr also shows the failure stage (`initial` or
`selection`), the response's stop reason (`n/a` if no response arrived), why recovery was
skipped, and validation errors as `schema:location (pydantic error type)`. The skip reason is
one of `no_response`, `response_not_end_turn`, `no_text_output`, `malformed_json`,
`candidate_validation_failed`, or `within_finding_limit`. When a report has too many findings,
validation reports only the count error, so the findings are also checked without the limit.
That way an invalid finding hidden behind the count error is still reported. Field names the
schema doesn't define appear as `<unknown field>`, since they come from the model. Raw output
and input values are never shown.

#### Multi-expert runner

```bash
uv run python -m blindspot.pipeline \
  --plan "I want to open a small coffee shop in Warsaw." \
  --perspective "Financial" \
  --perspective "Operations" \
  --perspective "Human & Behavioral"
```

Runs the single-expert call above once per perspective, concurrently. You choose the
perspectives (one to three; trimmed, blank or case-insensitive duplicates rejected before
anything is sent). Each expert sees only the plan and its own perspective, never another
expert's results, and there are no pipeline-level retries. stdout is one JSON object: an
overall `status` (`success` if every expert succeeded, `partial` if some did, `failed` if
none did — zero findings counts as success) and an `experts` list in input order, each
with its perspective, status, elapsed seconds, the model ID the API returned (`null` only
if no response arrived, so a truncated or invalid failure still shows it), either the
report or a sanitized error and `diagnostics` (as above; `null` on success), plus that expert's usage (calls, whether recovery ran, tokens, and
whether usage is incomplete). Findings are reported per expert as-is: not merged, ranked,
or deduplicated. stderr gets a summary of calls, summed tokens (marked incomplete where a
call returned no usage), and total elapsed time. Exit code is 0 only if every expert
succeeded.

**Billing:** each expert makes one request plus at most one recovery request, so **three
perspectives can make up to six billable requests.**

#### Model comparison

```bash
uv run python -m blindspot.compare \
  --plan "I want to open a small coffee shop in Warsaw." \
  --perspective "Operations" \
  --model "<first-model-id>" \
  --model "<second-model-id>" \
  --repeats 3
```

Runs the same plan and perspective through exactly two models you name, for manual side-by-side
inspection. `--repeats` is 1–3 (default 1). All arguments (and `ANTHROPIC_API_KEY`) are checked
before anything is sent; `ANTHROPIC_MODEL` is not needed, since `--model` overrides it. Runs are
sequential and independent, and the model that goes first alternates each repetition. Prompts,
schemas, token limits, timeouts, and the bounded recovery are the same as the single-expert
command. There are no comparison-level retries.

Each comparison gets its own directory, `runs/<UTC timestamp>-<random suffix>/` (git-ignored):

- `manifest.json`: the plan, perspective, model IDs, repetition count, UTC creation time,
  the exact expert and selection prompts, the output schemas sent, the request limits, and
  the git revision and dirty status (`null` if unavailable).
- `run-<order>-rep<n>-<model>.json`: one file per run, written as soon as that run finishes.
  Each holds the repetition and execution order, the requested and returned model IDs,
  success/failure, the validated report or a sanitized error and its `diagnostics`, elapsed time, call count,
  whether recovery ran, known token usage, and whether that usage is incomplete. Unknown
  values are `null`.

Completed files stay on disk if a later run fails or you interrupt the command. Credentials,
request headers, and raw invalid model output are never saved. **The saved files contain the
plan you supplied and the generated findings**, so treat `runs/` accordingly.

A failing run (refusal, invalid output, API error) is recorded and the comparison continues.
At the end, stdout shows per-model successes/failures, runs that needed recovery, known
input/output token totals (marked `(incomplete)` where some usage is unknown), average elapsed
time, and the results directory. The exit code is nonzero if any run failed. Output quality
and cost aren't scored automatically; the files are for reading.

**Billing:** each run makes one request plus at most one recovery request. One repetition
across two models makes **at most four** billable requests; three repetitions make **at most
twelve**.

A comparison this small is exploratory. It can show differences worth a closer look, but it
isn't evidence of a definitive ranking between models.

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
src/blindspot/prompts.py # expert and finding-selection system prompts
src/blindspot/expert.py  # single-expert async call (+ bounded selection recovery) + CLI
src/blindspot/pipeline.py  # concurrent runner for 1–3 explicit perspectives + CLI
src/blindspot/compare.py   # two-model comparison CLI (results saved under runs/)
src/blindspot/llm_smoke.py  # connection smoke-check CLI
tests/                   # pytest suite
docs/                    # design notes and the original project brief
```
