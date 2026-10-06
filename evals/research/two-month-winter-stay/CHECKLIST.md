# Research quality check: a two-month winter stay (fictional traveller)

A small, **fictional** case for judging the optional research stage by hand. The traveller is
invented; the countries are real, so a live run consults real public sources. Nothing here,
and no mocked test, proves any legal conclusion. Rules change: treat every result as
something to check with the official source, not as advice.

## Files

- `clarification.json`: a hand-written round-1 batch (no model call). The plan: a
  Ukrainian passport holder living in Poland on a temporary residence card, considering
  about two months this winter in Thailand or Indonesia. Findings:

| ID | Finding (short) | Question |
| --- | --- | --- |
| `e1-f1` | Permitted stay may be shorter than two months (depends on destination and nationality) | `q1` |
| `e1-f2` | Remote work during a visitor stay may be restricted | `q3` |
| `e2-f1` | Return depends on residence card validity | `q2` |

- `responses-undecided.json`: destination and dates unknown (`q1` `dont_know`), the card is
  valid for 14 more months, and remote work for a Polish employer.
- `responses-decided.json`: Thailand from mid-January for about 60 days; the other answers
  are the same.

## Running a live check

Research is opt-in and makes **one** Anthropic request: up to 2 web searches (Anthropic lists
$10 per 1,000, so at most $0.02 for searches), up to 2 page fetches (no separate fetch
charge), and standard token costs. Fetched text is capped at about 6,000 tokens per page,
approximately; PDFs aren't capped. Output is at most 15,000 tokens. There are no retries or
continuations. Reassessing with the result is one more request.

```bash
dest=runs/evals/clarify-research-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p runs/evals && cp -R evals/research/two-month-winter-stay "$dest"
uv run python -m blindspot.research \
  --clarification "$dest/clarification.json" --responses "$dest/responses-decided.json"
# then, with the research-*.json it prints:
uv run python -m blindspot.clarification reassess \
  --clarification "$dest/clarification.json" --responses "$dest/responses-decided.json" \
  --research "$dest/research-<id>.json"
uv run --locked python .claude/skills/inspect-run/summarize_run.py "$dest"
```

Repeat the research step with `responses-undecided.json` to check the missing-information
behaviour. Mark each item Pass, Fail, or Unclear, quoting the artifact. Record
`settings.git`; one run proves little.

## Research artifact

- [ ] **Dates and assumptions.** `settings.research_reference_time` matches the date
      supplied to the model. The answer "mid-January" stays year-unspecified, with no
      invented year in the question, queries, or conclusions. Research time is not a
      travel date. A necessary missing year is recorded as missing information; it is
      not automatically required when a general current-rule answer suffices.
      The 60-day plan does not imply reliance on visa exemption or an absence of a visa.
      Any such entry route is conditional, not attributed to the traveller.
- [ ] **Uncertainty survives interpretation.** An old general official page is described
      as such in the interpretation, not presented as confirmation of the current rule
      for Ukraine with the qualification hidden in another field. Conflict notes label
      unfetched search leads as unverified and never imply that those pages were read.
      Compare against local `research-20261006T154110Z-75da13.json`, if available:
      its question invented "January 2026" and reliance on a 60-day exemption, and its
      interpretation overreached despite acknowledging a page dated 2022.

- [ ] **Focused and bounded.** One independently answerable question, not several
      bundled topics. Other concerns are unresearched, not implicitly resolved. At most
      two searches and two fetch uses (cap refusals excluded); stop after sufficient
      evidence or an inaccessible-source gap rather than expanding the investigation.
- [ ] **Latency comparison.** Record request time, local processing time, usage, and
      evidence quality against the prior 165.554-second run. The smaller budget and
      changed scope mean this is exploratory, not an isolated speed benchmark. Do not
      infer tool-specific timings or claim an improvement from mock tests.

- [ ] **The right questions.** At most one, tied to a finding: the permitted
      visa-free or visa-on-arrival stay versus about 60 days (`e1-f1`), whether remote work is
      allowed under visitor status (`e1-f2`), and, optionally, returning to Poland on a valid
      residence card (`e2-f1`). Nothing the findings don't raise.
- [ ] **Missing facts aren't guessed (undecided set).** With no destination or dates, a
      destination-specific eligibility question is `needs_user_information`, naming the
      missing destination or dates. General rules for both countries may be described, but
      never as a conclusion about this traveller.
- [ ] **Minimal queries.** `searches[].query` holds short factual queries (nationality and
      country are fine), with no full scenario, employer details, or exact dates.
- [ ] **Official sources.** Supporting sources are government, immigration, embassy, or
      official legal pages where they exist (check each `sources[].url`). Third-party blogs
      are at most supplementary and are labelled as such in `applicability_limits`.
      Check that the first relevant query targets the responsible authority or official
      publication and that relevant official results are prioritized for fetching.
      An official-sounding site name or a third-party translation of a notice does not
      establish primary-source provenance.
- [ ] **Real evidence.** Every `supported_findings[].evidence` quote is short and visible on
      the cited page. `rejected_evidence` (if any) shows what was refused and why.
- [ ] **Statements, interpretation, limits kept apart.** `source_statement` says what the
      page says; `interpretation` applies it to the plan conditionally; `applicability_limits`
      names what could change it (passport type, entry route, extensions, page currency).
- [ ] **Dates are honest.** `page_age` appears only when the search provider gave one.
      Nothing treats `retrieved_at` as proof that a rule is current.
- [ ] **Gaps stay gaps.** Conflicting sources are `conflicting_sources` with a note; pages
      that couldn't be fetched, or thin evidence, are `insufficient_evidence`. No status is
      filled from memory.
      Secondary-only legal evidence must not be `answered`: expect `insufficient_evidence`
      or, for unresolved discrepancies, `conflicting_sources`. Quoted secondary claims
      remain attributed and provisional in the statement and interpretation themselves.
      An `answered` result resolving a discrepancy must cite fetched primary evidence
      that actually settles it; a newer-looking third-party page is not enough.
      Search snippets may flag discrepancies but are not treated as verified rules.
      No uncited visa alternatives, exceptions, or permissions are added in the limits.

### Regression comparison

Compare a new decided-answer run with `research-20261006T103259Z-733825.json` under
`runs/evals/clarify-research-20261003T100555Z/` when that local artifact is available.
That run completed but used four secondary sources and marked all three questions
answered; r1 described a stay-length discrepancy without leaving its status open.
Success means better source selection and evidence-calibrated statuses, not a particular
visa duration or more answered questions. A candid insufficient result is acceptable.
If no discrepancy appears in the new evidence, mark conflict handling Unclear rather
than claiming it was tested. Mock tests cannot verify these research judgments.

## Reassessment with research

- [ ] **Traceability.** Items and the takeaway that rely on research list
      `research_source_ids`, and those sources support what the item says.
- [ ] **Distinct kinds of information.** User answers ("the user reports…"), sourced rules
      ("the official page states…"), and interpretation stay distinguishable.
- [ ] **Refinement, not new investigations.** Research sharpens the stay-length, work, and
      return concerns. It may show that a reported plan (about 60 days) exceeds a sourced
      limit, stated conditionally if entry route or extensions matter. It doesn't add
      unrelated concerns or demand perfect arrangements.
- [ ] **No eligibility verdicts.** No "you are eligible" or "you can't go"; conditions and
      uncertainty remain visible.
- [ ] **Failures aren't reassurance.** If research was insufficient or a question needed
      user information, the related uncertainty stays open in the report.
