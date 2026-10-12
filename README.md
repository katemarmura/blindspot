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
clarification round over a saved auto-team run (`blindspot.clarification`), whose saved
batches can also be answered in a minimal browser screen (over a small HTTP API). No
cross-review or database yet.

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

#### Answering screen (browser)

A single page for answering a saved batch, one question at a time, and then, only if you
ask, assessing your answers. It uses the HTTP API below. Answering needs no API key and
makes no model calls; assessing makes one paid request on the server (see below).

```bash
uv run uvicorn blindspot.main:app --reload   # from the repository root; reads ./runs
```

Open http://127.0.0.1:8000/ui and enter a batch ID (its folder name, such as
`clarify-20261009T140042Z-6b0833`), or go straight to
`http://127.0.0.1:8000/ui?batch=<batch-id>`. To try it without touching your own batches, use
the copied fixture from the HTTP example below:
http://127.0.0.1:8000/ui?batch=clarify-kitten-example.

- **Each question** shows its perspective labels, why it matters, and a text box. "Save
  answer" (primary) needs text. "Not applicable" saves anything typed as an optional
  explanation. "I don't know" and "Save for later" save their status without text.
- **Saving:** each response is saved straight away, and the screen moves on only after
  the save succeeds. A failed save keeps your text and explains what to do; buttons are
  disabled while a save is in progress.
- **Resuming:** the batch ID is in the URL, so refreshing reopens the first unanswered
  question.
- **When nothing is unanswered**, you see a completion message. Questions saved for later
  stay marked "Saved for later" and can be revisited. "Review this round's answers" lets
  you change any of this round's responses.
- **Follow-up batches** show round-1 answers in a collapsed, read-only "Earlier answers"
  section.
- **"Assess my answers"** appears once every question in the round has a response. It
  runs the same reassessment as `reassess` (**one paid model request**, stated next to the
  button, never retried or started automatically). Questions saved for later are listed as
  not answered. If nothing usable is answered (everything saved for later), the screen
  explains why and offers no button. While it runs (up to about a minute), the page shows
  a waiting message and answers can't be changed, here or in another tab.
- **The result** shows the takeaway first (blind spot, why it mattered, status, and its
  explanation), labelled "Current assessment" after round 1 or "Conclusion of this review"
  after a follow-up. Remaining concerns, addressed concerns, and unresolved uncertainties
  are collapsible sections. It's saved as `reassessment-<id>.json` in the batch folder, like
  the CLI's. A failure explains what went wrong without showing model output, keeps your
  answers, and offers an explicit "Try again" (another paid request).
- **Refreshing never repeats a paid request**, but the screen doesn't reload past results:
  after a reload, a finished assessment is only in its saved file, and one still running
  is shown as "already running" until it ends. Leaving the page doesn't cancel a running
  assessment; its result is still saved.
- The screen doesn't create plans or run research or follow-ups; use the CLI. A follow-up
  batch's carried round-1 research is included automatically; other research files can
  only be added with `reassess --research`.

The page is plain HTML, CSS, and JavaScript modules in `src/blindspot/web/`, served by
FastAPI with no build step or npm packages. It has a strict content security policy, and
all saved text is shown as text, never as HTML. The colours and their contrast ratios are
listed at the top of `src/blindspot/web/assets/app.css`. The bright orange `#F97316` is
used for fills with dark text (6.2:1); the darker `#C2410C` is for focus rings and small
orange details (5.2:1 on white).

#### Answering over HTTP (local dev server)

The FastAPI app can show a saved batch (round 1 or a follow-up), save answers one at a
time, and assess them. Viewing and saving use the same file, validation, and atomic writes
as `answer` and need no API key. **Only the assessment endpoint calls a model** (one paid
request, with the server's own key); research and follow-ups stay CLI commands.

```bash
uv run uvicorn blindspot.main:app --reload   # from the repository root; reads ./runs
```

A batch is named by its folder, such as `clarify-20261009T140042Z-6b0833`. Find yours
with `ls -d runs/*/clarify-* runs/evals/*/clarify-*`. Only folders directly inside a run
folder under `runs/` (or `runs/evals/`) are found; paths aren't accepted.

The example below uses a copy of the committed round-1 kitten fixture, so saving doesn't
touch your own batches. Its `q1` asks the kitten's age and vaccinations:

```bash
mkdir -p runs/evals/api-example
cp -R evals/reassessment/kitten-before-holiday runs/evals/api-example/clarify-kitten-example

# View the questions, perspectives, and current statuses
curl -s http://127.0.0.1:8000/clarifications/clarify-kitten-example

# Save (or replace) one answer
curl -s -X PUT http://127.0.0.1:8000/clarifications/clarify-kitten-example/responses/q1 \
  -H 'Content-Type: application/json' \
  -d '{"status": "answered", "answer": "Ten weeks old; first vaccination done."}'
```

In a follow-up (round 2) batch, only that round's questions (`q4` and later) can be saved;
check `editable` in the `GET` response.

| Endpoint | What it does |
| --- | --- |
| `GET /clarifications/{batch_id}` | Batch ID, `clarification_id`, `round`, `plan`, `responses_saved`, `next_question_id` (first question of this round with no response), and `questions`: `id`, `round`, `question`, `why_it_matters`, `perspectives`, `source_finding_ids`, `status`, `answer`, `editable`; plus `assessment_running`. Never creates files. |
| `PUT /clarifications/{batch_id}/responses/{question_id}` | Body `{"status": …, "answer": …}`. Saves that one response in the batch's `responses.json` (created from the template if missing) and returns the updated batch. Other entries, the instructions, and the reference `question` fields are kept. Refused (409) while an assessment of the batch is running. |
| `POST /clarifications/{batch_id}/reassessments` | **One paid model request.** Body must be exactly `{"confirm_paid_request": true}`. Runs `reassess` on the batch's `clarification.json` and `responses.json` (a follow-up's carried research included), saves `reassessment-<id>.json`, and returns `reassessment_id`, `round`, `conclusion_status`, `report`, `closing_note`, `assessed_responses` (the answers used, both rounds in a follow-up), `answers_changed_during_assessment`, and `used_research`. One at a time per batch. |

- **Statuses** are the response-file ones: `answered` (text required), `dont_know` (no text),
  `not_applicable` (optional explanation), `deferred` (no text), or `null` for unanswered.
  `status` must be present; `null` clears a response. "I don't know" is a recorded answer;
  `null` means not answered.
- **Follow-up batches** list the round-1 questions with their recorded answers and
  `editable: false`. Saving one returns 409; only this round's questions can be answered.
- **Errors:** 404 for an unknown batch or question ID; 422 for an invalid answer (for
  example `answered` without text); 409 for an invalid or failed saved batch, an invalid
  response file (reported and never overwritten), a round-1 question in a follow-up, or two
  batches with the same folder name; 403 if the batch or its files resolve outside `runs/`
  (for example through a symlink).
- **Assessment** is refused before any request with 422 without the confirmation, 409 when
  nothing usable is answered, the files are invalid, or one is already running, and 503 when
  the server has no `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`. A failed call returns 502 with an
  explanation built from its diagnostics (never model text) and is saved like the CLI's.
  Nothing is retried.
- **Consistent answers:** while an assessment runs, `PUT` returns 409, so the saved
  `recorded_responses` match what was assessed. Edits made outside the API during the call
  (by hand or with the CLI) can't be blocked; the response then has
  `answers_changed_during_assessment: true`.
- **Limits:** the "running" guard lives in the server process (one uvicorn worker), not in
  the files, so a CLI `reassess` of the same batch isn't blocked. A finished result isn't
  served again; read the saved `reassessment-*.json`.
- The interactive API docs are at http://127.0.0.1:8000/docs.

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

Health check: `GET /health` → `200 {"status": "ok"}`. For the batch-answering endpoints, see
[Answering over HTTP](#answering-over-http-local-dev-server).

All checks run in CI on every push and pull request — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

### Verify before pushing

```bash
make verify
```

This runs the CI checks locally, in CI's order: `ruff check .`, `ruff format --check .`,
`mypy`, and `pytest`, each via `uv run --locked`, then the answering screen's checks with
Node (`node --check` on its script and `node --test tests/ui/state.test.mjs`; Node 20 or
later, no npm packages). It never formats, auto-fixes, or rewrites `uv.lock` (an
out-of-date lockfile makes it fail).
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
  Research selects one decisive external question per run; other concerns remain
  unresearched. Saved timings separate the API request from local evidence processing,
  excluding file writing; provider tool/generation timing is unavailable.
  A UTC reference time is supplied and saved for each research call. It dates the
  research, not the user's travel; unspecified dates and arrangements remain uncertain.
  - at most 1 research question;
  - 2 searches (Anthropic lists $10 per 1,000 searches, so at most $0.02);
  - 2 page fetches (no separate charge; fetched text counts as input tokens, about 6,000
    tokens per page, approximate, and PDFs aren't capped);
  - 15,000 output tokens and a 180 s timeout;
  - one request, with no retries, no continuation after a pause, and no repair.

  Search and fetch counts are enforced by Anthropic (`max_uses`) and checked again by
  Blindspot. Ordinary tool failures still count; attempts explicitly refused with
  `max_uses_exceeded` do not. Those refusals remain in the saved diagnostics, and the
  final report still goes through schema and evidence validation.
- **Evidence:**
  - Every quote must appear (allowing only whitespace differences) in the text of a page
    fetched in that request. Quotes that don't match, URLs that were never fetched, overlong
    quotes, PDFs, and non-public pages are recorded as rejected, never used as evidence.
  - Search snippets alone are never evidence.
  - Reassessment can cite only fetched sources with accepted evidence excerpts. A page
    merely appearing in the fetched inventory is not enough. Inconclusive research may
    still inform an uncertainty without a source citation; follow-up rounds enforce
    the same evidence requirement.
  - For legal/administrative questions, the prompt directs research toward official
    sources first. Secondary-only findings stay provisional, and unresolved conflicts
    stay open. These judgments require manual review; matching a quote does not verify
    the publisher's authority or the rule's current applicability.
  - Omitted research lists default to empty lists. Status-specific details remain
    required: findings for answered questions, missing facts for `needs_user_information`,
    and conflict notes for `conflicting_sources`.
  - Findings with missing or empty excerpts are discarded and recorded as
    `missing_excerpts`; supported findings survive. An answered question left without
    verified findings becomes `insufficient_evidence`.
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

### Comparing research runs (coding-agent skill)

`.claude/skills/compare-research/` compares two saved `research-*.json` files to judge
whether a change improved speed, evidence quality, or usefulness. It's read-only, makes no
API calls, and gives no numeric score.

- **Invoke it:** `/compare-research <older-research.json> <newer-research.json>`. Both files
  are required; it never picks a pair itself.
- **Structure (helper):** it checks the saved input snapshot (plan, findings, question
  wording, answers), prompt, model, limits, reference time, and git state. It then compares
  outcomes and performance, with absolute and percentage changes; missing metadata is
  "unavailable", never zero. Questions are matched by finding IDs, not position.
- **Meaning (skill):** official sources, honest statuses, invented dates or arrangements,
  interpretations that keep the source's date and scope, and leads versus fetched evidence.
- **Changed limits:** when question, search, or fetch budgets changed, it says the speed
  comparison isn't like-for-like.

```bash
uv run --locked python .claude/skills/compare-research/compare_research.py \
  runs/<...>/research-A.json runs/<...>/research-B.json
```

### Claude Code development workflow

The project now has an implementation → independent review → fix → re-review workflow.
`CLAUDE.md` tells the main Claude Code session to use it for implementation and bug-fix
requests. You can also start it explicitly:

```text
/develop Add the next approved UI feature
/develop review current changes
```

The second command reviews only; it does not authorize fixes. For implementation work,
the main session implements and coordinates three project subagents:

- `blindspot-reviewer` checks behavior, tests, and relevant documentation using
  `review-change`, without editing the project.
- `blindspot-fixer` fixes confirmed, in-scope findings and related tests/docs.
- `blindspot-designer` advises before material UI changes and checks the result against
  the brief. It uses source and supplied visual evidence; it has no browser tools itself.

Each agent has its own console colour (reviewer blue, fixer green, designer purple).
The main session announces each agent as it starts and relays its response (verdict,
findings, checks) when it returns, then carries on without waiting; these updates are
for monitoring, not approval. A review-only request still runs one reviewer pass.

Agents run sequentially, sharing the checkout. The loop allows at most two fix passes
and three reviews, then reports any unresolved findings instead of continuing forever.
Any edit after a review counts as a fix pass, whoever makes it; if a fix touches the UI,
the designer re-checks it before the re-review.
Code changes use `make verify`; documentation-only work uses accuracy/link checks and
`git diff --check`. Prompt quality and browser behavior are reported separately from
automated test results. The workflow doesn't commit, push, or run live Blindspot API calls.
Claude Code's agent work still consumes Claude Code usage, with models inherited from
the parent session and normal permission controls preserved.

#### Review gate hook

`.claude/settings.json` registers `.claude/hooks/review_gate.py` for four Claude Code
hook events:

- **Each prompt:** it records the state of the working tree and adds a one-line reminder
  to use `develop` for implementation requests.
- **Reviewer start/stop:** a `blindspot-reviewer` run that finishes with the tree
  unchanged marks that exact state as reviewed.
- **End of turn:** if files changed since your prompt and that state wasn't reviewed (and
  no reviewer is still running), it sends the session back to run the review.

It can't block forever: at most **two** times per prompt, then it lets the turn end and
shows a warning that the changes weren't reviewed. If the session answers a block by
explaining that no review applies and stops without changing files, that stop is allowed
with the same kind of warning; if it edits files instead, it is blocked again. The state
it compares is the content of your files (tracked and non-ignored untracked), not the
commit, so committing or stashing reviewed changes doesn't count as new changes. Your own
pre-existing changes don't trigger it, and neither do git-ignored files such as `runs/`.

To bypass it for one turn, use an explicit phrase in the prompt at the start of a
clause (the start of the prompt or a line, after punctuation such as `.`, `,`, `;`, or
`!`, or after "please", "and", "but", "so", "then", or "just"): "skip
review" / "skip the workflow" (also "bypass" or "pause", as in "pause the workflow"),
"don't review" / "do not use the develop workflow" (also with "run", "start", "do",
"need", or "bother with"), or anywhere "without review" / "without the workflow" at the
end of a clause, or "no review needed". Detection is a fixed phrase match
(`SKIP_PATTERN` in the hook), not an understanding of intent: descriptions such as "fix
the bug where no review results are shown" don't count. You can also start Claude Code
with `BLINDSPOT_REVIEW_GATE=off`. Errors let the turn end. State lives in
`.git/claude-review-gate/`, so nothing is tracked. Recording the state stages your
files into a throwaway index, which can add unreferenced copies of changed and untracked
files to `.git/objects` (`git gc` removes them later); with large untracked files, this
takes disk space. It checks that a review ran, not what the review concluded.

Restart Claude Code after installing or editing these agent definitions or hooks, then
use the explicit command for the first task. The files are in `.claude/agents/`,
`.claude/hooks/`, and `.claude/skills/develop/SKILL.md`. Apart from that hook, the
workflow is instruction-driven, not enforced by CI: it can be interrupted, and cannot
promise exhaustive review. The completion
report must show the actual review verdict, checks, fix passes, and limitations; missing
agents or unfinished checks mean incomplete work, not approval.

## Project layout

```text
src/blindspot/           # the blindspot package (src layout: importable only after install)
src/blindspot/main.py    # FastAPI app: GET /health, the batch-answering router, the /ui screen
src/blindspot/web/       # answering screen: index.html plus assets/ (CSS, JS modules; no build)
src/blindspot/batch_api.py  # HTTP view/answer endpoints for one saved clarification batch
src/blindspot/guided.py  # guided one-question-at-a-time answering CLI
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
tests/                   # pytest suite (tests/ui/: Node tests for the screen's logic)
docs/                    # design notes and the original project brief
```
