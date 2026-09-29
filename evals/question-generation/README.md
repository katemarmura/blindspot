# Question-generation quality checks (fictional)

Small, **fictional** fixtures for judging clarification questions by hand. Each folder holds
a hand-written auto-team `result.json` (plan, expert assignments, and findings; no model
calls were made) and a `CHECKLIST.md`. They exercise the real
`blindspot.clarification prepare` path.

These are quality criteria, not expected outputs. Different question choices and wording
can all pass. Automated tests only check that the fixtures load; they don't and can't judge
question quality.

| Folder | Context | Notable traps |
| --- | --- | --- |
| `pet-before-travel/` | Adopting a rescue dog two weeks before a work trip | Vaccination and other animals tempt one bundled question; where the dog stays is already in the plan. |
| `small-business/` | Opening a bakery | The Legal & Regulatory expert **failed** (missing coverage); the funding source and number of staff are already in the plan. |
| `software-delivery/` | An eight-week client portal | Five findings compete for three questions; the team size, deadline, and design date are already in the plan. |

## Running a live check

`prepare` is **one billable request** (up to 15,000 output tokens) and writes a `clarify-*`
folder inside the run folder. Copy the fixture into the git-ignored `runs/` first, so the
output doesn't land in the committed fixture. Keep the `team-` prefix so the `inspect-run`
helper recognizes the copy.

```bash
case=pet-before-travel
dest=runs/evals/team-$case-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p runs/evals && cp -R evals/question-generation/$case "$dest"
uv run python -m blindspot.clarification prepare --run "$dest"
uv run --locked python .claude/skills/inspect-run/summarize_run.py "$dest"
```

`prepare` prints each question with its perspectives and cited findings. The full output is
in `$dest/clarify-*/clarification.json`. Mark each item in the case's `CHECKLIST.md` Pass,
Fail, or Unclear, with a short quote. Record the prompt revision
(`question_generation.git`); output varies between runs, so one pass isn't proof.

## Checks for every case

- [ ] **One topic per question.** Each question asks for one fact, decision, or coherent
      arrangement that can be answered independently. Test: could its parts get different
      answers? A single arrangement with several details is fine; two separate topics joined
      together is not.
- [ ] **Nothing hidden in the explanation.** `why_it_matters` is one short explanatory
      sentence, with no extra questions or requests ("…and also confirm whether…").
- [ ] **Examples stay examples.** Examples in a question clarify it without becoming a
      checklist of separate things to answer.
- [ ] **Conditional follow-ups count as separate questions.** "…and if so, …", "…if not,
      …", or "…or will X be needed?" add a second question that can get a different answer
      (for example "yes" to the first part and "I don't know" to the second). That fails.
- [ ] **Prerequisites stand alone.** When one answer decides whether another question
      matters, only the prerequisite is asked. The dependent question is left for later,
      not attached.
- [ ] **Current state and solution aren't bundled.** A question about what exists or is
      planned doesn't also ask the user to choose or explain what they'll do about it.
- [ ] **Coherent arrangements are still fine.** One question about a single quantity,
      decision, or arrangement passes even with "and" or several details. For example,
      "What opening days and hours are you planning?" is one arrangement, not two
      questions. Judge by whether parts could be answered separately, not by punctuation.
- [ ] **Nothing already answered.** No question asks what the plan states, and no two
      questions ask the same thing.
- [ ] **Meaningful priority.** The questions chosen are the ones whose answers could most
      change the analysis. Lower-priority topics are left out rather than squeezed in.
- [ ] **Justified consolidation.** A question cites several findings only when the same
      answer bears on all of them, not merely because they're related.
- [ ] **Valid references.** Every cited ID exists (the helper reports no `UNRESOLVED`
      question citations), and the perspectives printed with each question fit what it asks.
- [ ] **Neutral wording.** Questions ask what exists or is planned; they don't imply the user
      has neglected something or that an unmentioned arrangement is absent.
- [ ] **Missing coverage isn't filled.** A failed perspective produces no invented questions.
      Zero questions is valid when clarification wouldn't materially help.
