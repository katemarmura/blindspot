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
| `responses-three-uncovered-days.json` | 5, 7 | Like the sufficient set, but q3 says the friend **and** the neighbour are both unavailable on the same three days, with no one else arranged. The user checks the camera twice a day. On the other seven days the friend visits and the neighbour, who has a key, can respond within an hour. |

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
- [ ] **Actors and scope are preserved.** Each action, instruction, permission, and
      availability stays with the person the answer gives it to. Durations and conditions are
      kept, and arrangements are described as reported mitigations, not guarantees (see
      case 7).

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

## Case 7: actors, authority, and duration are preserved (`responses-three-uncovered-days.json`)

This case also exercises case 5: the answers reveal a concrete gap, and it must stay open.

**Acceptable:**
- **The three uncovered days stay the central open concern.** On those days neither the
  friend nor the neighbour is available, and no one else is arranged. This appears in
  `remaining_considerations` (and at most once more as a specific missing fact), not buried.
- **The friend's role is correct.** The friend receives the vet's contact details, the
  written list of signs, and the written permission to seek and approve treatment.
- **The camera belongs to the user.** The user checks the camera twice a day. The friend and
  the neighbour don't.
- **The neighbour's role is limited to what was stated.** The neighbour has a key and can
  respond within an hour on the seven covered days. Nothing more is assumed: no written
  instructions, no treatment authority, no vet contact.
- **Separation is for the trip.** The report says for the whole trip, or during the trip,
  and doesn't make it permanent or unconditional.
- **Arrangements are mitigations.** They're acknowledged as reported mitigations of specific
  gaps (for example "the user reports…", "addresses the lack of…"). They're not presented as
  guarantees, or as having "removed" or "eliminated" a risk.
- **The other arrangements stay addressed.** The feeder, the vaccination booked for after the
  return, separation, and the friend's instructions remain acknowledged, with no new doubts
  about their reliability.

**Misattribution or overstatement:**
- "The friend and neighbour have vet contacts, a symptom list, spending authority…" (this
  gives the neighbour the friend's instructions and authority).
- Camera checks attributed to the friend or the neighbour, or left ambiguous in a sentence
  that lists the friend's and neighbour's roles.
- "Permanently separated", or dropping "for the whole trip" in a way that changes its
  meaning.
- "Removes the conflict risk", "eliminates", or "guarantees".
- Treating the neighbour as available on the three uncovered days, or the camera as an
  in-person response.

**Over-correction (also a failure):**
- Reopening addressed concerns, or questioning whether the user will really check the camera,
  whether the feeder will work, or whether the friend will follow the instructions, without
  a finding or answer that makes it relevant.
- Asking for the neighbour's qualifications or the camera's coverage just because they
  weren't stated.

To rerun this case (one billable request):

```bash
dest=runs/evals/clarify-kitten-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p runs/evals && cp -R evals/reassessment/kitten-before-holiday "$dest"
uv run python -m blindspot.clarification reassess \
  --clarification "$dest/clarification.json" \
  --responses "$dest/responses-three-uncovered-days.json"
```

## Case 8: attribution across rounds (follow-up after `responses-three-uncovered-days.json`)

A two-round case. Round 1 is case 7. Round 2 is a follow-up batch whose question asks
whether anyone else could respond in person on the three uncovered days. The fictional
round-2 answer is an explicit update:

> Since my previous answer, I have booked a pet sitter for those three days. They will visit
> daily and can respond in person within an hour if I notice a problem on the camera. They
> have a key, the vet's contact details, the written warning signs, and my permission to take
> either cat to the vet and approve treatment up to the agreed amount. The arrangements for
> the other seven days remain unchanged.

**Regression evidence (a saved run):**
`runs/evals/clarify-20260929T185145Z-aa3e2b/reassessment-20260929T185506Z-cb8540.json`.
Its `addressed_concerns[2]` says "the friend has vet contacts…, **checks a pet camera twice
daily, and has now booked a pet sitter**…". It gives the user's actions to the friend
through a shared grammatical subject. Review it with `/inspect-session` on that file.

**Duration regression in the next run:**
`runs/evals/clarify-20260929T185145Z-aa3e2b/reassessment-20260930T094356Z-b7432b.json`
corrects the camera attribution, but `addressed_concerns[0]` calls the setup
"A permanent, no-matter-what separation setup". The recorded `q2` answer says
"for the whole trip whatever happens": the commitment is unconditional during the trip,
not permanent. Mark duration preservation Fail for this output even though attribution
improved. These local saved runs are evidence, not committed fixtures or proof of consistent
model behavior.

**Acceptable:**
- **The user's actions stay with the user.** The user checks the camera twice a day and the
  user booked the sitter.
- **Each person keeps their own role.**
  - The **friend** visits daily on the seven covered days, with the vet's contact details,
    the written signs, and treatment authority.
  - The **neighbour** has a key and can respond within an hour on those seven days.
  - The **sitter** visits daily on the three days, has a key and the same information and
    authority, and can respond within an hour.
- **Summaries may leave detail out** (for example not mentioning the neighbour at all), but
  must not merge roles into a sentence that credits one person with another's actions.
- **Naming the person each time is fine, and so are separate sentences within one item.**
  Splitting one concern into several items just to separate people isn't required.
- **The sitter addresses the three-day gap** (`e2-f2`, `e3-f2`, citing `q3` and the
  follow-up question). The report treats "since my previous answer" as an update that
  replaces "I haven't arranged another person", not as a contradiction.
- **A summary like "covering in-person response across all ten days" is acceptable.** The
  recorded answers describe seven days of existing coverage plus three days of sitter
  coverage, so this is a faithful summary, not an unsupported claim.
- **Nothing is reopened.** The addressed concerns stay addressed (feeder, vaccination timing,
  separation, instructions), with no new doubts about the sitter's reliability or
  experience. Empty `remaining_considerations` and `unresolved_uncertainties` are acceptable.
- **Separation stays a reported mitigation.** It lasts for the whole trip, and phrases like
  "addresses the unsupervised-conflict concern" are fine.

**Wrong:**
- "The friend … checks a pet camera … and has booked a pet sitter", or any sentence whose
  shared subject gives the camera checks or the booking to the friend, neighbour, or sitter.
- Giving the neighbour instructions or treatment authority, or giving the sitter the
  seven-day role.
- Separation "removing" or "eliminating" the conflict risk, or being "guaranteed"; the saved
  run says "removing the unsupervised-conflict risk".
- Calling the separation "permanent" or extending it beyond the trip. "For the whole trip"
  preserves the reported duration without reopening the concern.
- Reopening the three-day gap without a reason taken from the answers, or treating the update
  as an unresolved conflict with round 1.

Judge meaning, not completeness: a shorter summary that keeps each actor correct passes; a
detailed one that misattributes a single action fails the attribution check.

To rerun round 2 against the saved follow-up batch (one billable request), see the
`reassess` command in the project README's clarification section, pointed at that batch's
`clarification.json` and `responses.json`.

## Structured conclusion evaluation

New reports use `takeaway` as a single object: `blind_spot`, `why_it_mattered`, `status`,
and `status_explanation`. All four must describe the same prioritized insight. The CLI
renders the fields and adds its closing acknowledgment when no concerns remain. The model
must not put a free-form closing recap into any field. Entirely empty reports use null.
Legacy text takeaways remain readable; the historical checks below document prior failures.

- [ ] The sitter update selects the in-person coverage gap, explains its consequence,
      marks it addressed, and identifies the sitter as the decisive change.
- [ ] The uncovered-days answers retain an open coverage gap and get no all-addressed
      closing acknowledgment.
- [ ] Each field stays brief and on-topic, without adding feeding/vaccination inventories.
- [ ] Status and explanation agree with detailed sections; labels alone do not prove this.
- [ ] No invented delays, shared permissions, permanent separation, or guarantees.

## Historical prose-takeaway checks

New reports end with a `takeaway`: a few short sentences explaining the key blind spots,
why they mattered, and their current status. It isn't an arrangement inventory or a new
investigation. Reassessments saved before this change have no takeaway (they're legacy, not
invalid), so judge those on their sections only. The CLI prints the takeaway on stderr as the
**current assessment** after round 1, and as the **conclusion of this review** after round 2.
Stdout stays the report JSON.

For every new report, check that the takeaway:

- [ ] **Agrees with the sections.** Its main point appears in `remaining_considerations` (or,
      when nothing material remains, the sections are empty or only addressed). It adds no
      concern, fact, number, or recommendation that isn't in the sections.
- [ ] **Explains an insight.** Names the gap, dependency, or assumption and why it matters,
      then says whether it is open or addressed. Doesn't merely list the user's preparations.
- [ ] **Doesn't invent an oversight.** A previously unstated arrangement isn't proof the user
      neglected it. Distinguish a later change from clarification of an existing arrangement.
- [ ] **Is short and readable.** A few short sentences in one or two brief paragraphs,
      not a list of every item.
- [ ] **Keeps people, durations, and uncertainty.** It doesn't merge people's roles, turn
      "for the trip" into something permanent, or state as fact something the sections treat
      as unknown.
- [ ] **Makes no guarantee.** Nothing like "the plan is safe", "all risks are covered", or
      "verified".

Specific cases:

1. **An open concern (case 7, `responses-three-uncovered-days.json`).** The takeaway names the
   three days without anyone available in person as the main remaining concern. Other
   arrangements aren't listed as new worries.
2. **Addressed concerns (case 8, the round-2 sitter update).** The takeaway explains the
   dependency between noticing a problem and having someone able to respond in person,
   and that the newly booked sitter addresses the three-day gap. Other resolved concerns,
   including trip-long separation, stay in the detailed sections. It closes
   with a bounded acknowledgment that no additional concerns remain among those examined,
   with no invented risks or doubts about the sitter. It
   doesn't claim the plan is guaranteed safe, and it keeps the sitter's and the user's roles
   distinct if it mentions them.
3. **An important unknown (case 1 or case 4, `responses-baseline.json`).** When vaccination
   timing or the separation setup is still unknown, the takeaway keeps that uncertainty ("it
   is not yet known whether…"). It doesn't resolve the question or turn it into a certain
   problem.
4. **Zero follow-up questions with a remaining concern.** If `followup` returns no questions
   while the preceding reassessment still has remaining considerations or unresolved
   uncertainties, the CLI says there's no useful follow-up and names that reassessment as
   the conclusion available for the session. It says those concerns remain, and stopping
   doesn't resolve them. No new reassessment file is created and the old one isn't
   rewritten.
5. **Agreement with the detailed sections (every case).** Read the takeaway last. Every claim
   in it should be traceable to an item in the sections, and the most consequential open
   item shouldn't be missing from it.

Not every omission is a failure: the takeaway prioritizes. A takeaway that leaves out a minor
item passes. One that leaves out the most consequential open item, or introduces something
new, fails.

### Regression: an insightful conclusion must not manufacture an open concern

Saved evidence:
`runs/evals/clarify-20260929T185145Z-aa3e2b/reassessment-20260930T104626Z-46126b.json`.
The takeaway usefully distinguishes noticing a problem from responding in person, but
reopens monitoring because the exact camera-check spacing is unspecified. It also says
detection "relies on" the camera despite the reported daily visits. Mark these as failures
of the stopping rule and of considering the arrangements together.

- [ ] With the sitter update, explain the monitoring/response dependency as addressed by
      the reported arrangements, without requiring a precise camera schedule or continuous
      observation unsupported by the findings. The result is a bounded assessment, not a
      guarantee of safety.
- [ ] With the three-uncovered-days answers, keep the actual missing in-person coverage
      open. Do not use the stopping rule to dismiss an explicitly reported gap.
- [ ] Do not call camera checks the only detection mechanism; daily visits also exist.
- [ ] Explain the key insight and its status, then stop rather than listing every resolved
      arrangement. Aim for at most three to five short sentences, fewer when sufficient.

These are manual quality judgments. The saved example is local regression evidence, not
an automated semantic test or proof that a prompt revision consistently improves outputs.

### Regression: unrelated resolved details appended to the takeaway

The observed sentence "The related concern about a young kitten outgrowing a once-daily
care routine is also addressed" then lists the feeder, caretaker, and vaccination timing.
It distracts from the monitoring/response insight and combines separate concerns.

- [ ] In the fully addressed sitter case, keep the takeaway focused on the key
      monitoring/response dependency and its status. Leave feeding and vaccination details
      in addressed_concerns rather than appending an "also addressed" sentence.
- [ ] Do not merge feeding frequency and vaccination timing into a single care concern
      merely because both concern the kitten.
- [ ] Preserve the source's meaning: whether the routine meets the kitten's needs is not
      a claim that the kitten is "outgrowing" it.
- [ ] Prioritize genuinely open concerns when present; this brevity rule must not hide an
      important unresolved issue.

### Regression: role inventories introduce unsupported permissions

In local saved evidence
`runs/evals/clarify-20260929T185145Z-aa3e2b/reassessment-20260930T151934Z-6ad485.json`,
the addressed summary gives both the friend and neighbour written warning signs and
vet-treatment authority. The recorded answers give these to the friend and sitter, not the
neighbour. The takeaway also appends a list of resolved topics to its closing sentence.

- [ ] Addressed summaries explain the gap and the decisive reported mitigation, with
      necessary conditions, rather than reconstructing everyone's resources and duties.
      For example, the sitter's availability addresses the three-day response gap; listing
      all permissions is unnecessary to explain that change.
- [ ] Omitting incidental detail is acceptable; inventing shared permissions or removing
      an important availability restriction is not.
- [ ] With the sitter update, close the takeaway with the bounded acknowledgment alone,
      without a list of resolved topics appended to it.
- [ ] With the three-uncovered-days answers, still identify the missing coverage as open;
      do not use the all-addressed closing sentence or brevity to conceal the gap.

### Regression: stale delay estimates and unrelated concerns combined

Local evidence:
`runs/evals/clarify-20260929T185145Z-aa3e2b/reassessment-20260930T152940Z-1c48fe.json`.
The takeaway claims a problem could remain unaddressed "for a full day"; the original
finding's estimate assumed no remote monitoring and does not establish that delay under
the recorded arrangements. The second addressed item also combines feeding and vaccination
timing because both appear in q1, despite their different dependencies.

- [ ] Do not import a prior delay estimate after the assumptions supporting it changed.
      Describe the known coverage gap without inventing a detection or response delay.
- [ ] Keep feeding needs and vaccination timing distinct even when citing the same answer.
      Combine findings only when they represent the same gap and mitigation.
- [ ] Explain that the sitter addresses the missing in-person response without repeating
      the full list of contacts, permissions, and warning signs in the takeaway.
