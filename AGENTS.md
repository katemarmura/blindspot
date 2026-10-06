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

Implemented so far, all manual (`blindspot.clarification`, below):
- one batch of up to three questions from a saved auto-team run;
- one reassessment from a user-edited JSON response file;
- **one** optional follow-up batch (round 2), started from a successful round-1
  reassessment and reassessed with the answers from both rounds.

Not yet implemented: one-question-at-a-time interaction, suggested answers, more than one
follow-up round, automatic stopping between rounds, and a tailored final conclusion.
Preserve the existing CLI as a baseline; do not speculatively add a UI, database,
framework, or automatic multi-round loop. Keep user answers distinct from expert
assumptions, and retain links to original findings.

## Current scope: minimal server, a single-expert experiment, a multi-expert runner (manual or auto-selected team), a model-comparison CLI, and a manual clarification round with one follow-up

**As of now, this repository contains a project scaffold, a bare FastAPI server, and one
small domain contract with one real LLM call (plus at most one bounded recovery call)
built on it. It runs alone, concurrently for 1–3 caller-supplied perspectives, for a
1–3 expert team proposed by one team-selector call (followed by one synthesis call), or
across two named models for manual comparison. A saved auto-team run can then go through
one clarification round (one question call, one reassessment call), plus at most one manual
follow-up round (one follow-up question call, one reassessment call).**
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
- `src/blindspot/clarification.py` — clarification over a saved auto-team run, as three
  explicit subcommands (`prepare`, `reassess`, and `followup`; see the follow-up bullet
  below). Each makes at most one call (60s, `max_retries=0`, no
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
    - Otherwise it makes one call (15000 tokens) with `synthesis.build_input` plus the
      existing synthesis, then checks every cited ID in Python (`unknown_source_id`).
    - The question prompt requires one independently answerable fact, decision, or
      arrangement per question. It forbids squeezing extra topics in to fit the limit or
      hiding requests in `why_it_matters`, and it allows consolidation only when one answer
      bears on several findings. Conditional follow-ups ("and if so…") count as separate
      questions: only the prerequisite is asked, and the current state isn't bundled with the
      user's intended solution. There's no punctuation-based filter; this is prompt guidance. Judge changes with the manual fixtures in
      `evals/question-generation/` (their `README.md` and `CHECKLIST.md`s), which
      `tests/test_question_generation_fixtures.py` keeps loadable.
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

    Its report shape is the three sections plus a required `takeaway` field:
    `ConclusionInsight` (`blind_spot`, `why_it_mattered`, `status`, `status_explanation`),
    or null only when all sections are empty. Text fields are nonblank and extra fields
    are forbidden. Status is open, partly_addressed, or addressed. The insight prioritizes
    one gap; its semantic accuracy still requires manual evaluation.
    - Saved reports use `SavedReassessmentReport`, which accepts a structured insight,
      legacy prose, or a missing takeaway. No conversion or rewriting of saved runs occurs.
    - `render_conclusion` displays labelled fields and adds the bounded closing sentence
      only when no remaining considerations or unresolved uncertainties exist. Legacy
      prose stays labelled as legacy. This uses no model call.
    - **Reassessment files** gain `conclusion_status`: `current_assessment` (round 1),
      `review_conclusion` (round 2), or `null` on failure.
    - **CLI messages:** `reassess` completion messages go to stderr, with stdout staying
      the report JSON. `followup` prints questions and its zero-question conclusion message
      to stdout; usage and saved-file paths go to stderr.
      - The takeaway is labelled as the current assessment or the review conclusion.
      - After a zero-question follow-up, the preceding reassessment is named as the
        available conclusion (with an open-concerns note), and the generation metadata
        records `no_useful_followup` and `conclusion_reassessment_file`. No extra call is
        made and nothing is rewritten.
      - A failure, or nothing new to reassess, announces no new assessment.

    Its attribution rules: keep each action, instruction, permission, and availability with
    the person it belongs to, and never transfer them to someone else. When an item
    summarizes several people's arrangements, name the person whenever it changes, so a
    shared grammatical subject can't carry one person's actions over to another; use
    separate sentences in the item if needed, and don't split the concern into several
    items. Preserve durations and
    conditions, so temporary never becomes permanent. Describe arrangements as reported
    mitigations, not guarantees. None of this is grounds to reopen addressed concerns.

    It also requires titles that keep their item's uncertainty and no point repeated across
    sections, and it forbids inferring unstated dates, timing, or layout and hardening
    conditional findings.

    The rules stay general, with no domain-specific wording. Judge prompt changes with the
    manual fixture in `evals/reassessment/` (its `CHECKLIST.md`), not with mocked tests.
  - **Optional research (`research.py`, `python -m blindspot.research`):**
    - **Integration:** opt-in. It uses Anthropic server tools (`web_search_20250305`,
      `web_fetch_20250910`) in ONE request on the existing key and model; no new
      dependency or credential. The research call requests a final JSON object and
      validates it locally against `ResearchReport` (`schemas.py`). It doesn't use
      `output_config`, because the docs don't state compatibility with server tools.
    - **Limits (`LIMITS`):** 1 question, 2 searches, 2 fetches (`max_uses`, rechecked from
      the response blocks), about 6,000 fetched tokens per page (provider-controlled and
      approximate; PDFs uncapped), 15,000 output tokens, a 180 s timeout, `max_retries=0`.
      `pause_turn`, refusal, and truncation are failures, never continued. The local
      count excludes only attempts with a matching `max_uses_exceeded` tool result;
      ordinary failures and attempts with missing results still count. Tool outcomes
      and provider-reported usage remain unchanged in the saved artifact.
      New live runs enforce one question; older reports with up to three remain loadable.
      `timings` separates request time from local processing (null without a response),
      excluding artifact writing. Provider per-tool/generation timing is unavailable.
      The input includes a UTC research reference time, saved in
      `settings.research_reference_time`; this is not a user travel date. Prompt rules
      preserve unspecified dates/arrangements and source uncertainty in interpretations.
    - **Provenance (`collect` and `verify`):**
      - Only fetched public HTTP(S) plain-text pages become sources (`s1`…). PDFs are
        "unverifiable" and non-public URLs are rejected.
      - A quote must be found in the page's first `MAX_VERIFY_CHARS` after whitespace
        collapsing only.
      - Quotes that don't match, unfetched URLs, overlong quotes, and excess excerpts go to
        `rejected_evidence`, with no quote text or URL kept.
      - A question with no verified finding is downgraded to `insufficient_evidence`, and
        unknown finding IDs fail the stage.
      - Missing or empty `excerpts` reject that finding with `missing_excerpts`, preserving
        supported findings. Malformed excerpts still fail schema validation.
      - Omitted `findings`, `missing_information`, and `conflict_notes` default to empty
        lists. Status-specific requirements still apply: answered needs findings,
        needs_user_information needs missing facts, and conflicting_sources needs notes.
      - Page text isn't stored.
    - **Safety and lineage:** charges are confirmed interactively unless `--yes` is given.
      Credentials load only after the inputs validate. `answers_snapshot()` (in
      `clarification.py`) is saved as `input_snapshot`: batch ID and round, `plan_sha256`,
      `findings_sha256` (every expert's assignment and report), `question_ids`,
      `questions_sha256` (full questions of every round), and the recorded answers verbatim.
      Hashes use sorted-key compact JSON; a mismatch message names the differing parts.
    - **Use in reassessment (`reassess --research`):**
      - `load_research_evidence` rejects a failed file, a snapshot mismatch, or inconsistent
        source IDs before any call.
      - The input gains a labelled evidence section.
      - Report items and `ConclusionInsight` gained an optional `research_source_ids`
        (omitted from JSON when empty, via `exclude_if`). `_reassessment_reference_issues`
        checks it against the research sources, and rejects any citation when no research
        was supplied.
      - The saved reassessment gets a `research` block (`ResearchEvidence.link()`) only when
        research was used.
    - **Follow-up lineage:** `load_followup_source` reads the reassessment's `research`
      block, reloads the file (`_followup_research`), re-runs `load_research_evidence`
      against the batch and that reassessment's recorded responses, and requires the same
      research ID and source IDs. Report citations are then checked against it. Any failure
      refuses the follow-up before any call or directory. The evidence goes into the
      follow-up input, and the round-2 artifact stores it as `prior_research`
      (`ResearchContent`, omitted when None). The artifact validator requires that it covers
      every research citation in `prior_reassessment`. Round-2 `reassess` uses it
      automatically (`carried_forward=True`), and both `reassess --research` and
      `run_research` refuse such a batch.
    - **Tests and evals:** `tests/test_research.py` uses real SDK block types in fake
      responses. The manual case is `evals/research/two-month-winter-stay/`.
      The research prompt targets official sources first for legal/administrative claims,
      keeps secondary-only conclusions provisional, and preserves unresolved conflicts.
      These are model judgments evaluated with the checklist, not guarantees of the
      quote verifier or a domain-name classifier.
  - **`answer --clarification … [--responses …] [--revisit-deferred]` (`guided.py`):**
    guided, one-question-at-a-time answering of a round-1 or round-2 batch.
    - **Input and output:** a small `Terminal` protocol with `ask` and `say`. `StdTerminal`
      wraps `input` and `print`; tests script it.
    - **Checks before any write:** it validates the artifact (`load_artifact`) and an
      existing response file (`load_responses`). An invalid file is reported and never
      replaced, and a missing one is created from `_template`.
    - **Saving:** each response is checked with `ResponseEntry` and written immediately with
      `write_json`, preserving other entries and metadata. Artifacts and reassessments are
      never touched. q, end of input, or Ctrl+C stop without recording the current
      question.
    - **Resuming:** it skips `answered`, `dont_know`, and `not_applicable`, and skips
      `deferred` unless `--revisit-deferred` is passed. It makes one pass per session, with
      no cycling.
    - **Reassessment:** only on an explicit `r`, after a one-billable-request notice. It
      calls `reassess()` and shows the result with `render_conclusion`. Configuration is
      loaded only then, so answering needs no credentials. It never moves on to a follow-up
      round by itself.
    - **Covered by** `tests/test_guided_cli.py`.
  - **`followup --reassessment …/reassessment-*.json` (round 2, manual, at most once):**
    - `load_followup_source` validates everything before any call or file is created:
      - the reassessment is a successful round-1 one with a report;
      - its artifact is found (the recorded `clarification_file`, else the sibling
        `clarification.json`) and its `clarification_id` matches;
      - the saved `recorded_responses` plus `unanswered_question_ids` cover the batch's
        questions exactly;
      - the report's references resolve.
      A round-2 reassessment or artifact is rejected ("only one follow-up round").
    - It makes one call (`FOLLOWUP_QUESTIONS_SYSTEM_PROMPT`, `QUESTIONS_MAX_OUTPUT_TOKENS`).
      The input is the original context, the round-1 questions, the reassessment's **saved**
      recorded responses (never the possibly edited `responses.json`), the unanswered IDs,
      and the report, labelled as a model interpretation.
    - It writes a new `clarify-*` folder next to the round-1 one. The artifact has
      `round: 2`, `previous` lineage (`ClarificationLineage`), and copies of
      `prior_questions`, `prior_responses`, `prior_unanswered_question_ids`, and
      `prior_reassessment`. Its own `questions` continue the numbering (`q4`…), so IDs are
      unique across rounds. Zero questions prints "No useful follow-up", which is not a
      claim that the plan is risk-free.
    - Round-1 artifacts are still written in the old format (the round-2 fields are
      omitted), and older files load with `round: 1`.
    - `reassess` on a round-2 artifact:
      - the response file may answer only round-2 questions, so round-1 answers can't be
        overwritten;
      - "no new context" is judged on this round alone;
      - the input lists both rounds' questions and recorded responses tagged by round (the
        round-1 report isn't included);
      - references and "addressed" checks use both rounds' questions and answers;
      - the saved file has `clarification_round: 2` and round-tagged `recorded_responses`.
  - Diagnostics use the `clarification_questions`, `reassessment`, and `followup_questions`
    stages (errors are `ClarificationError`); invalid input files raise
    `ClarificationInputError`.
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
  `tests/test_clarification_followup.py` covers the follow-up round with a fake client:
  generation, IDs across rounds, lineage, saved-answer provenance, round-2 reassessment with
  accumulated answers, rejection before any call (invalid lineage, third rounds), zero
  questions, failures, the round-1 file format, and both helpers on round-2 files. The
  manual case lives in `evals/followup/`.
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
- `compare-reassessments` compares two saved reassessments. It establishes what changed
  (recorded answers, exact prompt text, model, limits, git), classifies the pair, then
  judges improvements, regressions, and persistent issues by meaning and cited IDs. Its
  helper, `compare_reassessments.py`, reuses `summarize_run.py`'s parsing and is tested in
  `tests/test_compare_reassessments_helper.py`.
- `inspect-session` (`/inspect-session <path>`) reviews one whole clarification session
  (round 1, and the follow-up round if present). It finds the session only through saved
  lineage (a round-2 artifact's `previous`) and never by date or folder. Its helper,
  `inspect_session.py`, reuses `summarize_run.py`'s parsing. It reports each call once, the
  carried-forward answers (checking that earlier answers weren't dropped or changed),
  current response files that changed, reference checks, defects, and limitations. The
  semantic review stays in the skill's instructions. Tested in
  `tests/test_inspect_session_helper.py`.

These are development aids, not Blindspot runtime capabilities. If an artifact format
changes, update `inspect-run`'s format table and both helpers.

## Conventions

- Keep `pyproject.toml` and `uv.lock` in sync — run `uv lock` after manual edits to
  `pyproject.toml`, and commit the lockfile.
- New runtime dependencies: `uv add <package>`. Dev-only tooling: `uv add --dev <package>`.
- The user maintaining this project is an experienced TypeScript/JavaScript engineer
  currently learning Python — when behavior or conventions differ from typical JS/TS
  tooling in a non-obvious way, a short note is welcome, but don't over-explain
  well-known Python basics.
