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
that runs one to three perspectives concurrently. You either name them yourself or let one
team-selector call propose them (`--auto-team`, which also synthesizes the results).
There's also a small model-comparison command (`blindspot.compare`) and one manual
clarification round over a saved auto-team run (`blindspot.clarification`). No cross-review,
UI, or database yet.

**Planned experience:** guided clarification, with one question at a time and a short
conclusion informed by the user's answers. This is not implemented in the current CLI.
See the [product direction update](docs/project-brief.md#product-direction-update--2026-09-24)
for the intended flow and open design questions.

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

##### Automatic team selection

```bash
uv run python -m blindspot.pipeline \
  --plan "I want to open a small coffee shop in Warsaw." \
  --auto-team
```

`--auto-team` replaces `--perspective`; you can't use both. First, one request asks the
configured model to propose the smallest useful team of one to three experts. Each expert
gets a perspective, an objective, and one to three neutral questions. Perspectives come from
a built-in catalogue (Financial, Legal & Regulatory, Operations & Logistics, Timeline &
Dependencies, Human & Behavioral, Health & Safety, Security & Privacy, Technical, Social &
Relationships, Environmental, Travel & Mobility), or the model can make up a custom one. The
team is validated locally: 60s timeout, 1500 output tokens, no retries, no repair. If
selection fails (refusal, truncation, invalid team, API error), no expert runs and the
failure is reported with diagnostics.

Otherwise the team is printed to stderr and its experts run concurrently, as above. Each
expert sees the plan and only its own perspective, objective, and questions. The objective
and questions guide attention; they don't dictate conclusions. A successful selection
doesn't guarantee successful experts, so the result can still be `partial` or `failed`.

stdout JSON adds `team`, `team_selection` (status, model, error, diagnostics, usage), each
expert's `assignment`, and `total_usage` covering the selector and all experts. stderr
reports the selector's usage separately and includes it in the totals. Results are saved
under `runs/team-<UTC timestamp>-<random suffix>/` (git-ignored):

- `team.json`: the plan, git state, selector settings, and the selected team, or the
  selection failure. It's written before any expert starts, so it survives an interruption.
- `experts.json`: the expert results and the source-ID mapping. It's written before
  synthesis starts, so it survives a synthesis failure or interruption.
- `result.json`: the full final JSON, including synthesis.

**These files contain your plan and the generated findings.**

**Synthesis.** After the experts finish, one more request turns the successful experts'
findings into at most five distinct considerations (60s timeout, 3000 output tokens, no
retries or repair).

- Each supplied finding gets a stable ID, `e<team position>-f<finding position>`, for
  example `e1-f2`. Each synthesized finding must cite at least one.
- IDs are checked in Python: citing an unknown ID makes synthesis fail. `sources` in the
  JSON maps each ID back to its expert and finding, and the original expert reports are
  kept unchanged next to the synthesis.
- **A valid ID shows which findings were cited, not that the rewritten text faithfully
  represents them.** Check synthesized findings against their sources yourself.
- Failed experts appear in the synthesis input only as `unavailable` perspectives (missing
  coverage), never with their error text.

`synthesis.status` is separate from the top-level expert status:

- `completed`: the call succeeded.
- `no_findings`: experts succeeded but found nothing, so an empty report is returned
  without a call.
- `unavailable`: team selection failed, or every expert failed.
- `failed`: the call failed; expert reports are still returned, along with the model,
  usage, and diagnostics.

A partial expert failure stays visible in `status` and `experts` even when synthesis
succeeds. The exit code is 0 only if every expert succeeded and synthesis is `completed` or
`no_findings`.

**Billing:** one selector request, up to three experts with at most one recovery each, and
one synthesis request: **at most eight billable requests**.

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

#### Clarification round (manual, with one optional follow-up)

After an `--auto-team` run, you can answer a few questions and get a reassessment. There are
two explicit steps, each making **at most one billable request** with no retries:

```bash
# 1. Generate up to three questions from a saved run
uv run python -m blindspot.clarification prepare --run runs/team-<id>

# 2. Edit runs/team-<id>/clarify-<id>/responses.json, then:
uv run python -m blindspot.clarification reassess \
  --clarification runs/team-<id>/clarify-<id>/clarification.json \
  --responses runs/team-<id>/clarify-<id>/responses.json
```

`prepare`:

- It reads the run's `result.json` and asks the model for up to three questions whose
  answers could materially change the analysis. It can also decide no questions would help.
- Each question asks for one thing you can answer on its own: a fact, a decision, or a
  single arrangement. Separate topics aren't bundled to fit the three-question limit; the
  lower-priority ones are left out. A question cites several findings only when one answer
  bears on all of them, and the "why it matters" line carries no hidden extra requests.
  Conditional follow-ups ("and if so, …") aren't attached: when one answer decides whether
  another question matters, only the first is asked.
- It uses the plan, the successful experts' findings, failed perspectives (as missing
  coverage), and the synthesis if there is one.
- Each question cites the finding IDs it bears on (for example `e1-f2`), and its
  perspectives are shown next to it.
- If the run has no successful findings, it stops without a call.
- It writes a new `clarify-<UTC>-<id>/` folder inside the run folder, containing
  `clarification.json` (the context, questions, and call details) and a `responses.json`
  template. The run's own files aren't touched.

In `responses.json`, set each question's `status` to one of:

- `answered`, with `answer` text;
- `dont_know`;
- `not_applicable`, with an optional explanation in `answer`;
- `deferred`.

Leave `status` as `null` (or delete the entry) to skip a question.

**Guided answering (instead of editing the JSON).** You can answer a batch's questions one
at a time in the terminal. It works for round-1 and follow-up batches, and needs no API key:

```bash
uv run python -m blindspot.clarification answer \
  --clarification runs/team-<id>/clarify-<id>/clarification.json \
  [--responses runs/team-<id>/clarify-<id>/my-answers.json] [--revisit-deferred]
```

1. **Each question shows** its number in the batch, its perspectives, and why it matters.
   Choose `a` (answer now; text required), `d` (I don't know), `n` (not applicable, with an
   optional explanation), `l` (save for later, recorded as `deferred`), or `q` (quit).
2. **Every response is saved straight away**, as a validated, atomic write to the response
   file (`responses.json` next to the batch unless you pass `--responses`).
   - A missing file is created from the template.
   - An existing invalid file is reported and never replaced.
   - Quitting, Ctrl+D, or Ctrl+C keeps what you've already saved; a half-typed answer isn't
     recorded.
3. **Run the same command again to continue.** Answered, `dont_know`, and `not_applicable`
   questions are skipped. Deferred ones are skipped too unless you pass `--revisit-deferred`,
   and each question is asked at most once per session. To correct an answer, edit the
   response file.
4. **At the end**, you see what's complete, deferred, and unanswered. If any answer could
   change the assessment, choose `r` to reassess now (**at most one billable request**,
   using the same validation and saving as `reassess`) or `x` to exit with your answers
   saved.
   - The result is shown as the current assessment (round 1) or the conclusion of this
     review (round 2).
   - A follow-up batch is still a separate `followup` command.
   - A batch with no questions, or with only deferred or unanswered ones, makes no call.

`reassess`:

- It validates both files. Unknown question IDs, duplicate responses, and a status that
  doesn't fit the answer text are rejected before any call.
- If every question is unanswered or deferred, there's nothing new to reassess, so it makes
  no call.
- Otherwise it sends the original plan, findings, questions, and your exact responses in
  one request. It prints up to five remaining considerations, the concerns your answers
  address, and the unresolved uncertainties, each citing finding and question IDs (checked
  in Python).
- The result is saved as a new `reassessment-<UTC>-<id>.json` next to `clarification.json`.
  Your response file is not modified.

Reassessment updates the original concerns rather than searching for new ones. Each
section has one job:

- **Remaining considerations:** original concerns that are still open, stated as
  consequences or decisions.
- **Addressed concerns:** what an answer specifically improved. Learning a fact isn't enough.
- **Unresolved uncertainties:** a specific missing fact, and the decision it would change.

Each new report has a structured **takeaway** with `blind_spot`, `why_it_mattered`,
`status` (`open`, `partly_addressed`, or `addressed`), and `status_explanation`. It focuses
on one prioritized insight. The CLI displays those fields with labels and adds a fixed,
bounded closing sentence only when no remaining considerations or uncertainties exist.
For an entirely empty report, the model returns `takeaway: null` rather than inventing an
insight. The same reassessment call generates everything; no extra summarization call runs.

Older saved reports with a prose takeaway or no takeaway remain readable. Their prose is
labelled as legacy, not converted into invented structured content. New model responses
must use the structured format; reassessment stdout remains JSON.

- **After round 1**, the CLI prints it on stderr as the *current assessment*, with the
  command for the optional follow-up.
- **After round 2**, it's the *conclusion of this review*.
- **If the follow-up finds no useful question**, the CLI names the preceding reassessment
  as the conclusion available and says whether concerns remain. No extra call is made.
- **A failed call, or nothing new to reassess**, announces no new assessment.

Saved files gain `conclusion_status` (`current_assessment`, `review_conclusion`, or `null`).
Older reports still load and can start a follow-up. Stdout stays the report JSON, now with `takeaway`.

The prompt tells the model:

- to accept a concrete arrangement you report as answering the gap it covers, rather than
  replacing it with new doubts about its quality or the people involved;
- that missing detail alone isn't a reason to keep a concern open;
- to still report a real problem your answer reveals;
- to stop once the concerns are covered, so empty sections are valid (which doesn't mean the
  plan is guaranteed safe);
- to keep each action, instruction, permission, and availability with the person you gave
  it to (naming the person whenever it changes within a summary), keep durations as stated (for the trip, not permanent), and describe arrangements
  as reported mitigations rather than guarantees;
- to mention each point once, with titles that don't claim timing the body treats as unknown;
- not to infer dates, timing, or a place's layout that you didn't state;
- not to harden a conditional finding into a categorical claim;
- to read an `answered` response by what it says ("I don't know yet" is still uncertain).

**One follow-up round (optional).** After a successful reassessment you can ask for one
focused follow-up batch. It's 1 billable request to generate the questions, and 1 more to
reassess once you've answered them:

```bash
# 3. Generate up to three follow-up questions from a successful reassessment
uv run python -m blindspot.clarification followup \
  --reassessment runs/team-<id>/clarify-<id>/reassessment-<id>.json

# 4. Edit the new runs/team-<id>/clarify-<id2>/responses.json, then:
uv run python -m blindspot.clarification reassess \
  --clarification runs/team-<id>/clarify-<id2>/clarification.json \
  --responses runs/team-<id>/clarify-<id2>/responses.json
```

- **What it asks about:** only what the reassessment left open, where an answer could
  change the conclusion. It won't repeat round-1 questions: "I don't know" counts as an
  answer, and deferred questions aren't re-asked just because they're open. It won't reopen
  addressed concerns or start a new investigation.
- **Zero questions** means "no useful follow-up", not that the plan is risk-free.
- **Numbering:** follow-up questions continue the numbering (`q4`, …), and the new folder
  records which batch and reassessment it came from.
- **Answers come from the saved reassessment:** it uses the answers that reassessment
  actually recorded, even if you've edited the old `responses.json` since. Earlier files are
  never changed.
- **The follow-up reassessment** considers your answers from both rounds, each labelled
  with its round and status. The new `responses.json` covers only the new questions, so
  round-1 answers can't be overwritten.
- **Only one follow-up round:** asking for another from a follow-up's reassessment is
  rejected before any call.

Your answers are treated as reported context, not verified facts. "I don't know" and
deferral leave uncertainty open rather than proving a risk, and "not applicable" records
your assessment without automatically resolving related concerns. Each step reports only
its own token usage. **These files contain your plan, the findings, and your answers.**

Each saved question or reassessment step also records `call_diagnostics`, whether it
succeeded or failed:

- the requested model and output-token limit, and the elapsed time;
- whether a response arrived, and if so its model, stop reason, and content-block types
  and counts;
- the total characters across text blocks;
- whether the text used for parsing was valid JSON syntax (`null` if there was no text).
  This is separate from schema validation: a truncated response can be valid JSON and still
  count as a failure.

If no response arrived, those response fields are `null` rather than looking like an
empty response. Only counts and types are saved, never the returned text or thinking
content. A failed step prints a short call/response summary to stderr.

## Development commands

| Command | Purpose |
| --- | --- |
| `make verify` | Run all local checks, the same as CI (see below) |
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

### Verify before pushing

```bash
make verify
```

This runs the CI checks locally, in CI's order, through the uv-managed environment:
`ruff check .`, `ruff format --check .`, `mypy`, and `pytest`, each via `uv run --locked`. It
never formats, auto-fixes, or rewrites `uv.lock` (an out-of-date lockfile makes it fail).
It makes no API calls. Every check runs even if an earlier one fails; failures are listed at
the end, and the exit code is nonzero.

### Optional current-source research (manual, opt-in, billable)

When a concern depends on external facts that change, such as entry rules, permitted
stays, or official requirements, you can run **one** bounded research step over a
clarification batch and its recorded answers, then feed the result into reassessment. The
default workflow doesn't change; nothing researches unless you ask.

```bash
# 1. Research (asks you to confirm the charges first; --yes skips the prompt)
uv run python -m blindspot.research \
  --clarification runs/team-<id>/clarify-<id>/clarification.json \
  [--responses runs/team-<id>/clarify-<id>/responses.json] [--allowed-domain gov.example]

# 2. Reassess with the evidence (one more request)
uv run python -m blindspot.clarification reassess \
  --clarification runs/team-<id>/clarify-<id>/clarification.json \
  --responses runs/team-<id>/clarify-<id>/responses.json \
  --research runs/team-<id>/clarify-<id>/research-<id>.json
```

- **How it works.** It uses Anthropic's server-side web search and web fetch tools, inside
  one API request, with your existing `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. There's no
  extra credential or dependency. The fetching happens on Anthropic's servers: it only
  fetches URLs already in the conversation and refuses private addresses (per Anthropic's
  docs). Blindspot also drops any non-public or non-HTTP(S) source. It's read-only: no
  forms, logins, or bookings.
- **Limits and cost:**
  - at most 3 research questions;
  - 4 searches (Anthropic lists $10 per 1,000 searches, so at most $0.04);
  - 4 page fetches (no separate charge; fetched text counts as input tokens, about 6,000
    tokens per page, approximate, and PDFs aren't capped);
  - 8,000 output tokens and a 180 s timeout;
  - one request, with no retries, no continuation after a pause, and no repair.

  Search and fetch counts are enforced by Anthropic (`max_uses`) and checked again by
  Blindspot.
- **Evidence:**
  - Every quote must appear (allowing only whitespace differences) in the text of a page
    fetched in that request. Quotes that don't match, URLs that were never fetched, overlong
    quotes, PDFs, and non-public pages are recorded as rejected, never used as evidence.
  - Search snippets alone are never evidence.
  - A question missing an essential fact about you (destination, dates, nationality, work
    plans) is recorded as `needs_user_information` rather than guessed.
  - Conflicts and thin evidence stay visible.
- **The saved `research-<id>.json`** (next to the batch; nothing else is modified) records:
  - its lineage, and a snapshot of exactly what it was made from: the batch and round,
    hashes of the plan, the expert findings, and the full question wording, and the
    recorded answers;
  - questions tied to finding IDs;
  - sources (ID, URL, title, retrieval time, and `page_age` only when the search provider
    gave one);
  - verified quotes, with what the source states kept apart from the interpretation and the
    applicability limits;
  - missing information, conflicts, rejections, and fetch outcomes;
  - settings, the prompt, limits, usage, and diagnostics.

  It doesn't store page text. Retrieval time is not proof that a rule is current, and a
  valid source ID shows traceability, not a faithful interpretation.
- **Reassessment with `--research`:**
  - It accepts the file only if it succeeded and its snapshot matches this batch exactly
    (plan, findings, question wording, and recorded answers); otherwise it's rejected before
    any call, and the error names what differs.
  - Report items and the takeaway cite `research_source_ids`, which are checked against
    that file.
  - A failed or insufficient research result is never read as "no concerns".
  - Without `--research`, reassessment and its JSON are unchanged.
- **Follow-up after a researched reassessment:**
  - `followup` reloads the research that reassessment used and re-checks it against the
    batch and the answers that reassessment recorded. If the file is missing, replaced, or
    stale, the follow-up is refused before any call.
  - The question generator receives that evidence. The round-2 batch keeps a copy as
    `prior_research`, so its round-1 citations keep their evidence.
  - Round-2 reassessment includes the carried research automatically, labelled as made for
    the round-1 answers. Its `research` block shows `carried_forward_from_round: 1`.
  - A second research artifact for such a batch isn't supported yet: `research` and
    `reassess --research` refuse it before any request.

A fictional manual check lives in `evals/research/two-month-winter-stay/`.

### Follow-up round quality check (manual, live)

`evals/followup/kitten-after-first-round/` is a small **fictional** case: a round-1 batch
plus a hand-written successful reassessment whose answers mix `answered`, `dont_know`, and
`deferred`. Its `CHECKLIST.md` describes good follow-ups (open prerequisites; no repeats; no
re-asking deferred questions; no new investigation), how to copy it into `runs/evals/` and
run it (one billable request), and what to check after reassessing round 2.

### Question-generation quality check (manual, live)

`evals/question-generation/` holds three small **fictional** fixtures: a pet before travel,
a small business, and a software delivery. Each is a hand-written auto-team `result.json`
with a `CHECKLIST.md`. They exercise the real `prepare` path without earlier live runs. The
folder's `README.md` has the shared checks (one independently answerable topic per question,
no hidden requests, no repetition, sensible priority, justified consolidation, valid
references, neutral wording) and explains how to copy a fixture into `runs/evals/` before
running it (one billable request per fixture). Automated tests only check that the fixtures
load and that `prepare` runs on a copy; they don't judge question quality.

### Reassessment quality check (manual, live)

`evals/reassessment/kitten-before-holiday/` is a small **fictional** fixture: a clarification
artifact, five answer sets, and a `CHECKLIST.md` of eight cases (the eighth covers attribution across a follow-up round). The answer sets are: vague
baseline answers, answers with concrete arrangements, answers that reveal an unmet
dependency, sufficient answers that may give an empty report, and a three-uncovered-days
variant for checking that actors, authority, and durations are preserved. Each case lists acceptable
behaviour and examples of moving the goalposts. It's for judging live
reassessment output by hand after prompt changes. The checklist explains how to copy it
into `runs/evals/` and run it (one billable request per answer set). Automated tests only
check that the fixture stays loadable; they don't and can't judge output quality.

### Inspecting saved runs (coding-agent skill)

`.claude/skills/inspect-run/` is a project-level Claude Code skill for reviewing artifacts
under `runs/`. It's for development, not part of Blindspot's runtime.

- **Invoke it:** ask Claude Code to "inspect this run", "check the output", or "did
  reassessment use my answers correctly?", or type `/inspect-run`.
- **What it reports:** stage outcomes, and usage kept separate for the original run and each
  clarification attempt. It then traces conclusions back to cited findings and recorded
  responses, and labels observations as confirmed defects, hypotheses, or judgments.
- **Read-only:** it never modifies artifacts, reads `.env`, or makes API calls.

Its helper also works on its own for a quick structural summary (read-only, standard
library only):

```bash
uv run --locked python .claude/skills/inspect-run/summarize_run.py --list
uv run --locked python .claude/skills/inspect-run/summarize_run.py runs/<run-dir>
```

### Inspecting a clarification session (coding-agent skill)

`.claude/skills/inspect-session/` reviews one whole saved clarification session: round 1
and, if present, the follow-up round. It's a read-only development aid with no API calls
and no numeric score.

- **Invoke it:** type `/inspect-session <path>`, where the path is a `reassessment-*.json`,
  a `clarification.json`, or a `clarify-*` folder.
- **Finding the session:** it follows the saved lineage from round 2 back to the exact
  round-1 batch and reassessment. It never picks files because they're newer.
- **Structural checks:** it verifies question IDs, references, and that earlier answers
  were carried forward unchanged, using recorded answers rather than later edits to
  `responses.json`.
- **Content review:** it checks the questions, answers, and conclusions for repetition,
  whether new answers are applied to the right gaps, whether people and durations are
  kept, and whether mitigations are described without guarantees.

Its helper can also run on its own:

```bash
uv run --locked python .claude/skills/inspect-session/inspect_session.py <path>
```

### Comparing reassessments (coding-agent skill)

`.claude/skills/compare-reassessments/` compares two saved reassessments. It's also a
development aid: read-only, no API calls, and no numeric score.

- **Invoke it:** ask Claude Code to "compare the last two reassessments" or "did the prompt
  change help?", or type `/compare-reassessments [A.json B.json]`.
- **Choosing the pair:** with no paths, it takes the latest two in the relevant
  clarification batch and names them.
- **Establishing what changed:** before judging quality, it works out what differed between
  the two runs' inputs:
  - the answers each run actually used;
  - the exact saved prompt text;
  - the model, token limit, and git state.

  It then classifies the pair as a prompt change, an answer change, run-to-run variation,
  or multiple changes.
- **Judging the output:** it matches concerns by meaning and cited finding IDs and reports
  improvements, regressions, and persistent issues, each labelled verified, judgment, or
  hypothesis.

Its helper can also run on its own:

```bash
uv run --locked python .claude/skills/compare-reassessments/compare_reassessments.py \
  runs/<...>/reassessment-A.json runs/<...>/reassessment-B.json
uv run --locked python .claude/skills/compare-reassessments/compare_reassessments.py --batch runs/team-<id>/clarify-<id>
```

## Project layout

```text
src/blindspot/           # the blindspot package (src layout: importable only after install)
src/blindspot/main.py    # FastAPI app (currently just GET /health)
src/blindspot/config.py  # ANTHROPIC_API_KEY / ANTHROPIC_MODEL loading
src/blindspot/schemas.py # Finding / ExpertReport, ExpertDefinition / TeamPlan, Synthesis* (Pydantic)
src/blindspot/prompts.py # expert, finding-selection, team-selector, synthesis, clarification prompts
src/blindspot/expert.py  # single-expert async call (+ bounded selection recovery) + CLI
src/blindspot/pipeline.py  # concurrent runner: explicit perspectives or --auto-team + CLI
src/blindspot/compare.py   # two-model comparison CLI (results saved under runs/)
src/blindspot/team.py      # perspective catalogue + one-call team selector
src/blindspot/synthesis.py # source IDs + one-call synthesis (auto-team only)
src/blindspot/runs.py      # shared helpers for saving results under runs/
src/blindspot/clarification.py  # clarification: prepare, reassess, one follow-up round
src/blindspot/research.py  # optional, bounded current-source research (web search + fetch)
src/blindspot/llm_smoke.py  # connection smoke-check CLI
tests/                   # pytest suite
docs/                    # design notes and the original project brief
```
