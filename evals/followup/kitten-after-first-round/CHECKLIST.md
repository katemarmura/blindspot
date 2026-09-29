# Follow-up round quality check: kitten after the first round (fictional)

A small, **fictional** case for judging one follow-up batch (round 2) by hand. It is not an
automated test; mocked tests only check lineage and data integrity.

## Files

- `clarification.json`: the round-1 batch, a copy of
  `evals/reassessment/kitten-before-holiday/clarification.json`. It has six conditional
  findings and questions `q1`–`q3`.
- `reassessment-20260101T000000Z-fixture.json`: a hand-written, successful round-1
  reassessment (no model call was made). Its recorded answers are:

| Question | Status | Recorded answer |
| --- | --- | --- |
| `q1`: kitten's age and vaccinations | `answered` | 1.5 months old; microchip and first vaccination; more due at 2.5 months |
| `q2`: separation space | `dont_know` | none |
| `q3`: what the friend is told and may decide | `deferred` | none |

Its report leaves open: care frequency (`e2-f1`, conditional), the separation fallback
(`e1-f1`, `e1-f2`), problems between visits (`e2-f2`, never asked in round 1), vaccination
timing (`e3-f1`, no dates), and the friend's instructions (`e3-f2`, deferred). Nothing is
marked addressed, because the age alone is context.

## Running a live check

`followup` is **one billable request** (up to 15,000 output tokens). Copy the case into the
git-ignored `runs/` first. The new round-2 batch is created next to the copied round-1
folder:

```bash
dest=runs/evals/followup-kitten-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$dest" && cp -R evals/followup/kitten-after-first-round "$dest/clarify-round-1"
uv run python -m blindspot.clarification followup \
  --reassessment "$dest/clarify-round-1/reassessment-20260101T000000Z-fixture.json"
uv run --locked python .claude/skills/inspect-run/summarize_run.py "$dest"/clarify-2*
```

Mark each item below Pass, Fail, or Unclear, with a short quote. Record the prompt revision
(`question_generation.git`); output varies between runs.

## What good follow-ups look like

- [ ] **Open concerns only.** Questions target what the reassessment left open, where an
      answer could change a conclusion. Likely good targets:
  - the reference date for vaccination timing, for example "How old will the kitten be on
    the day you leave?" or "On what date is the next vaccination due?" (a prerequisite, one
    fact);
  - problems between visits (`e2-f2`), which round 1 never asked about, for example "Will
    anyone be able to check on the cats between your friend's visits?"
- [ ] **No repeats.**
  - `q2` isn't asked again in the same or a reworded form ("What space would you use to keep
    them apart?"); "I don't know" is a recorded answer.
  - A clearly different, answerable fact that could change the separation conclusion is
    acceptable (for example "Is there a room besides the bedroom whose door closes?"),
    but judge whether it's really new.
- [ ] **Deferred isn't re-asked automatically.** `q3` (what the friend is told and may
      decide) isn't asked again just because it's open.
- [ ] **No reopening and no new investigation.** No questions on topics the findings don't
      raise (costs, insurance, the adult cat's health). No questions that treat the age
      answer as resolving anything.
- [ ] **One topic each.** No "and if so…", no current state bundled with a solution, and
      nothing hidden in `why_it_matters`.
- [ ] **References and IDs.** New questions are numbered from `q4`. They cite original
      finding IDs that resolve (the helper shows no `UNRESOLVED`), and the printed
      perspectives fit.
- [ ] **Count.** Up to three. Zero would be surprising here, because answerable prerequisites
      exist, but isn't automatically wrong if the output says why. It's reported as "no
      useful follow-up", not as the plan being risk-free.

## After answering the follow-up

Fill in the new `responses.json` (only the round-2 questions appear), then run
`reassess --clarification <new>/clarification.json --responses <new>/responses.json`. That's
one more billable request. Then check:

- [ ] The reassessment uses both rounds' answers (`recorded_responses` in the saved file
      lists `q1`–`q3` with round 1 and the new questions with round 2).
- [ ] `q2` stays `dont_know` and `q3` stays deferred unless a round-2 answer genuinely
      covers them; round-1 answers aren't reinterpreted as something else.
- [ ] If a round-2 answer conflicts with a round-1 answer, the conflict is kept as an
      uncertainty rather than silently resolved.
- [ ] Addressed concerns cite only questions that were actually answered (or marked
      not applicable).
