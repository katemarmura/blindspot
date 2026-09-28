# Reassessment quality check: kitten before a holiday (fictional)

A small, **fictional** fixture for judging reassessment output by hand. It's based on a real
run, trimmed to what these checks need. It is not an automated test, and mocked tests can't
show that the model meets it: only a live output, read by a person, can.

The checks judge whether the reassessment treats the supplied answers consistently: that it
acknowledges progress, keeps real gaps open, and stops. They don't judge whether the cat-care
claims are true; none have been verified, and the answers are fictional user reports.

## Files

`clarification.json` is a valid clarification artifact, written by hand (no
question-generation call was made). It holds the plan and six conditional findings:

| ID | Finding (short) | Asked about in |
| --- | --- | --- |
| `e1-f1` | The introduction may still be in progress at departure | `q2` |
| `e1-f2` | The separation fallback depends on unstated space | `q2` |
| `e2-f1` | One daily visit may undercount kitten care (depends on age) | `q1` |
| `e2-f2` | No stated plan for problems between visits | `q3` |
| `e3-f1` | Vaccination schedule versus departure (depends on dates) | `q1` |
| `e3-f2` | The visitor's ability to notice problems and act | `q3` |

Answer sets, from least to most complete:

| File | Cases | What it supplies |
| --- | --- | --- |
| `responses-baseline.json` | 1, 4 | Age without dates, "I don't know yet" on separation, only a vet contact. |
| `responses-arrangements.json` | 2, 3, 4 | The same age answer, a closable bathroom with the kitten's own resources, and a warning-sign list with written authority to get treatment. |
| `responses-unmet-dependency.json` | 5 | Like the arrangements set, but the friend is away for three of the ten days and nobody covers them. |
| `responses-sufficient.json` | 6 | Concrete arrangements for every gap: the kitten's age at adoption, an automatic second meal, the next vaccination booked for after the return, separation for the whole trip, a pet camera, and a neighbour with a key. |

## Running a live check

Each reassessment is **one billable request** (up to 15,000 output tokens). Copy the fixture
into the git-ignored `runs/` first, so the output doesn't land in the committed fixture. Run
one answer set per command:

```bash
dest=runs/evals/clarify-kitten-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p runs/evals && cp -R evals/reassessment/kitten-before-holiday "$dest"
uv run python -m blindspot.clarification reassess \
  --clarification "$dest/clarification.json" --responses "$dest/responses-arrangements.json"
uv run --locked python .claude/skills/inspect-run/summarize_run.py "$dest"
```

Then read each `reassessment-*.json` in `$dest` and mark each item below Pass, Fail, or
Unclear, with a short quote and the section it came from. You can also ask Claude Code to run
`/inspect-run` against `$dest` with this checklist. Judge the meaning, not the wording.

Not every extra uncertainty is wrong. One is acceptable when it names a specific missing fact
that the findings or answers make relevant, and says which decision it would change. Record
the prompt revision (`settings.git`) with your results; output varies between runs, so one
pass isn't proof.

## Checks for every answer set

- [ ] **Citations resolve.** The helper reports no `UNRESOLVED` or `ADDRESSED BY NON-ANSWER`
      lines.
- [ ] **Sections have distinct roles.**
  - `remaining_considerations` are open or partly addressed original concerns, stated as
    consequences or decisions.
  - `addressed_concerns` say what specifically improved.
  - `unresolved_uncertainties` name a missing fact and the decision it would change.
- [ ] **Each point appears once.** The same open point isn't in both
      `remaining_considerations` and `unresolved_uncertainties`. A consequence is stated once,
      and only the specific missing fact is listed separately.
- [ ] **Titles match their bodies.** No title states a timing, overlap, or outcome that the
      item or another section treats as unknown.
- [ ] **Concise.** Items are one short sentence each, and sections aren't padded to their
      limits.
- [ ] **Sources are kept apart.** User answers are attributed ("the user reports"), and the
      model's own inferences are conditional.
- [ ] **No new investigation.** Nothing appears that none of the six findings raised, and no
      new numbers or advice.

## Case 1: baseline answers leave meaningful gaps open (`responses-baseline.json`)

**Acceptable:**
- The separation setup (`q2`, "I don't know yet") stays unresolved, even though its status
  is `answered`.
- Knowing the kitten's age gives context but doesn't resolve the care concern (`e2-f1`).
- "Only the vet's contact details" can partly address who to contact. It leaves open what
  the friend should watch for and what they may decide (`e3-f2`).

**Moving the goalposts, or the wrong direction:**
- Listing the age alone as an addressed concern.
- Declaring the care concern certain ("one daily visit cannot meet its needs"), or calling
  the kitten "younger than assumed" when `e2-f1` already assumed it was young.
- Concluding from "one-bedroom apartment" that there's no space to separate the cats.
- Treating "only the vet's contact details" as unknown instructions, when "only" means none
  were given.

## Case 2: concrete arrangements are acknowledged (`responses-arrangements.json`)

**Acceptable:**
- The closable bathroom with the kitten's own resources is recognized as answering the
  separation gap (`e1-f2`, and the fallback part of `e1-f1`).
- The warning-sign list and written authority to get treatment are recognized as answering
  what the friend watches for and may decide (`e3-f2`).
- Both appear in `addressed_concerns`, briefly and specifically.

**Moving the goalposts:**
- New uncertainties about the bathroom's size, suitability, or comfort for a stay of that
  length, when no finding depended on them.
- New uncertainties about the friend's experience, skill, or practical ability, beyond the
  instructions and authority reported.
- New requirements the findings never raised, such as "behavioural milestones" or readiness
  criteria for deciding when the cats can share space. These are stricter versions of
  `e1-f1`.
- Still saying there is "no separation plan" or that the friend "only has a vet contact".

## Case 3: unchanged concerns may remain, but not as new (`responses-arrangements.json`)

**Acceptable:**
- Checks still happen only once a day, so problems starting between visits (`e2-f2`) may
  stay as a remaining consideration, stated as the finding stated it.
- The same applies to care frequency (`e2-f1`), kept conditional.

**Moving the goalposts, or wrong:**
- Presenting these as newly discovered ("new concern", "additionally", "it also emerges").
- Re-raising a concern the answers covered, under a new name.
- Dropping the visit-frequency concern while claiming the arrangements resolved it (they
  don't change how often anyone checks).

## Case 4: timing stays conditional without dates (baseline and arrangements sets)

**Acceptable:**
- Whether the next vaccination falls before, during, or after the trip stays unresolved,
  because no dates tie "1.5 months" to adoption or departure. A short uncertainty naming
  the missing date is fine.

**Wrong:**
- Any title or body saying the vaccination falls "during", "near", or "close to" the trip.
  For example, a title like "Vaccination series incomplete during the trip window" alongside
  an uncertainty saying the timing is unknown.

## Case 5: a revealed unmet dependency keeps the concern open (`responses-unmet-dependency.json`)

**Acceptable:**
- The report says that, by the user's account, no one visits on three of the ten days. It
  keeps that open against `e2-f2` and `e3-f2`, and it's the most prominent remaining item.
- The bathroom and the instructions can still be acknowledged, as in case 2. Revealing one
  gap doesn't erase other progress.
- Noting that this conflicts with the plan's daily visits, stated conditionally.

**Wrong:**
- Omitting or softening the three uncovered days to keep the report short.
- Treating the written instructions as covering those days.
- Turning the gap into general doubt about the friend's reliability.

## Case 6: sufficient answers may give an empty report (`responses-sufficient.json`)

**Acceptable:**
- `remaining_considerations` and `unresolved_uncertainties` are empty, or have at most one
  brief item that names a specific dependency the answers leave unmet.
- `addressed_concerns` briefly list what improved: separation for the whole trip, the
  automatic second meal, the vaccination booked after the return, the camera and neighbour
  for problems between visits, and the instructions with authority.
- No claim that the plan is now guaranteed safe.

**Moving the goalposts:**
- Doubting the feeder's, camera's, or neighbour's reliability, or the bathroom's
  suitability, without a finding or answer that makes it relevant.
- Asking for further detail (camera coverage, room size, the neighbour's experience) just
  because more detail is possible.
- Keeping the vaccination-timing uncertainty when the answer states it's booked for after
  the return.
- Filling the sections to their limits.
