// Unit tests for the answering screen's pure logic. Run: node --test tests/ui/state.test.mjs
// No DOM, network, or dependencies; Node's built-in test runner only.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assessmentEligibility,
  buildUpdate,
  conclusionHeading,
  createNavigation,
  describeAssessmentError,
  describeLoadError,
  describeFollowupError,
  describeSaveError,
  editableQuestions,
  followupAvailability,
  followupOutcome,
  followupRequestBody,
  initialText,
  formatSavedDate,
  isOutdated,
  isPlausibleBatchId,
  needsReassessment,
  NO_TAKEAWAY_TEXT,
  nextQuestionId,
  outdatedNote,
  priorQuestions,
  progress,
  resultKicker,
  SAVED_ASSESSMENT_UNAVAILABLE,
  savedAssessmentNote,
  savedAssessmentUpdate,
  statusLabel,
  takeawayKind,
  takeawayStatusLabel,
} from "../../src/blindspot/web/assets/state.mjs";

const q = (id, status = null, { editable = true, answer = null } = {}) => ({
  id,
  round: 1,
  question: `${id}?`,
  why_it_matters: "Because.",
  perspectives: ["Financial"],
  source_finding_ids: ["e1-f1"],
  status,
  answer,
  editable,
});

const batch = (...questions) => ({ round: 1, plan: "A fictional plan.", questions });

test("answer requires nonblank text and is trimmed", () => {
  assert.match(buildUpdate("answered", "   ").error, /Type an answer/);
  assert.match(buildUpdate("answered", undefined).error, /Type an answer/);
  assert.deepEqual(buildUpdate("answered", "  Six months.  ").body, {
    status: "answered",
    answer: "Six months.",
  });
});

test("not applicable keeps an optional explanation", () => {
  assert.deepEqual(buildUpdate("not_applicable", "").body, {
    status: "not_applicable",
    answer: null,
  });
  assert.deepEqual(buildUpdate("not_applicable", " No lease. ").body, {
    status: "not_applicable",
    answer: "No lease.",
  });
});

test("I don't know and save for later never send text", () => {
  for (const action of ["dont_know", "deferred"]) {
    assert.deepEqual(buildUpdate(action, "typed text").body, { status: action, answer: null });
  }
  assert.ok(buildUpdate("something_else", "").error);
});

test("next question skips responses, including deferred, and wraps around", () => {
  const b = batch(q("q1", "answered"), q("q2"), q("q3", "deferred"), q("q4"));
  assert.equal(nextQuestionId(b), "q2");
  assert.equal(nextQuestionId(b, "q2"), "q4");
  assert.equal(nextQuestionId(b, "q4"), "q2");
  assert.equal(nextQuestionId(batch(q("q1", "dont_know"), q("q2", "deferred"))), null);
});

test("prior-round questions are never next and never editable", () => {
  const b = batch(q("q1", null, { editable: false }), q("q4"));
  assert.equal(nextQuestionId(b), "q4");
  assert.deepEqual(editableQuestions(b).map((x) => x.id), ["q4"]);
  assert.deepEqual(priorQuestions(b).map((x) => x.id), ["q1"]);
  assert.equal(nextQuestionId(batch(q("q1", null, { editable: false }))), null);
});

test("progress counts this round only, with deferred as a response", () => {
  const b = batch(
    q("q1", "answered", { editable: false }),
    q("q4", "deferred"),
    q("q5"),
    q("q6", "not_applicable"),
  );
  assert.deepEqual(progress(b), { total: 3, responded: 2, deferred: 1, unanswered: 1 });
});

test("deferred is labelled as saved for later, never as answered", () => {
  assert.equal(statusLabel("deferred"), "Saved for later");
  assert.equal(statusLabel("dont_know"), "I don't know");
  assert.equal(statusLabel(null), "Not answered yet");
});

test("the text box is prefilled only with answers and explanations", () => {
  assert.equal(initialText(q("q1", "answered", { answer: "Yes." })), "Yes.");
  assert.equal(initialText(q("q1", "not_applicable", { answer: "N/A." })), "N/A.");
  assert.equal(initialText(q("q1", "not_applicable")), "");
  assert.equal(initialText(q("q1", "deferred")), "");
});

test("load errors: missing, invalid file, network, server", () => {
  assert.equal(describeLoadError({ status: 404, detail: "x" }).title, "Batch not found");
  const invalid = describeLoadError({ status: 409, detail: "Response file is invalid." });
  assert.equal(invalid.title, "This batch can't be opened");
  assert.equal(invalid.message, "Response file is invalid.");
  assert.equal(describeLoadError({ network: true }).retry, true);
  const server = describeLoadError({ status: 500, detail: "Traceback (most recent call)" });
  assert.doesNotMatch(server.message, /Traceback/);
});

test("save errors keep the user's text and never show raw server output", () => {
  const validation = describeSaveError({
    status: 422,
    detail: [{ msg: "Field required", loc: ["body", "status"] }],
  });
  assert.match(validation, /Field required/);
  assert.match(validation, /still here/);
  assert.match(describeSaveError({ network: true }), /didn't respond/);
  const server = describeSaveError({ status: 500, detail: "Internal Server Error\nTraceback" });
  assert.doesNotMatch(server, /Traceback/);
  assert.match(server, /still here/);
});

test("batch IDs follow the API's folder-name rule", () => {
  assert.ok(isPlausibleBatchId("clarify-20261009T140042Z-6b0833"));
  for (const bad of ["", "../runs", "clarify-", "clarify-a/b", "team-1", "clarify-*"]) {
    assert.equal(isPlausibleBatchId(bad), false, bad);
  }
});

test("a newer navigation makes earlier loads and saves stale", () => {
  const navigation = createNavigation();
  const loadA = navigation.begin();
  const saveA = navigation.current();
  assert.ok(navigation.isCurrent(loadA) && navigation.isCurrent(saveA));

  const loadB = navigation.begin(); // opening B, or going back/forward
  assert.equal(navigation.isCurrent(loadA), false);
  assert.equal(navigation.isCurrent(saveA), false);
  assert.ok(navigation.isCurrent(loadB));
});

test("starting a navigation aborts the previous load but saves have no signal", () => {
  const navigation = createNavigation();
  const first = navigation.begin();
  assert.equal(navigation.current().signal, null);
  navigation.begin();
  assert.equal(first.signal.aborted, true);
});

test("assessment needs one usable response; deferred ones are counted, not usable", () => {
  const onlyDeferred = assessmentEligibility(batch(q("q1", "deferred"), q("q2", "deferred")));
  assert.equal(onlyDeferred.eligible, false);
  assert.match(onlyDeferred.reason, /saved for later/);
  const mixed = assessmentEligibility(batch(q("q1", "dont_know"), q("q2", "deferred")));
  assert.deepEqual(mixed, { eligible: true, deferred: 1, reason: null });
  assert.equal(assessmentEligibility(batch(q("q1", "not_applicable"))).eligible, true);
  // Round-1 answers in a follow-up don't make this round assessable.
  const round2 = batch(q("q1", "answered", { editable: false }), q("q4", "deferred"));
  assert.equal(assessmentEligibility(round2).eligible, false);
  assert.equal(assessmentEligibility(batch()).eligible, false);
});

test("round 1 is a current assessment and round 2 a review conclusion", () => {
  assert.equal(conclusionHeading("current_assessment"), "Current assessment");
  assert.equal(conclusionHeading("review_conclusion"), "Conclusion of this review");
  assert.equal(takeawayStatusLabel("partly_addressed"), "Partly addressed");
});

test("assessment errors: server messages pass through, others never leak raw output", () => {
  assert.equal(
    describeAssessmentError({ status: 502, detail: "The model declined." }),
    "The model declined.",
  );
  assert.match(describeAssessmentError({ network: true }), /may still have run/);
  const raw = describeAssessmentError({ status: 500, detail: "Traceback: secret" });
  assert.doesNotMatch(raw, /Traceback/);
  assert.match(raw, /another paid request/);
});

// --- Reopening a saved assessment (fictional data) ---

const report = (overrides = {}) => ({
  remaining_considerations: [],
  addressed_concerns: [],
  unresolved_uncertainties: [],
  takeaway: null,
  ...overrides,
});

const saved = (overrides = {}) => ({
  batch_id: "clarify-x",
  reassessment_id: "20260101T000000Z-aaa111",
  created_at: "2026-01-01T12:00:00+00:00",
  round: 1,
  conclusion_status: "current_assessment",
  report: report(),
  closing_note: null,
  assessed_responses: [],
  based_on_earlier_answers: false,
  used_research: false,
  ...overrides,
});

test("the unavailable note appears only for a failed read or unusable files", () => {
  assert.equal(savedAssessmentNote(null), SAVED_ASSESSMENT_UNAVAILABLE);
  assert.equal(savedAssessmentNote({ assessment: null, unusable_files: 2 }), SAVED_ASSESSMENT_UNAVAILABLE);
  assert.equal(savedAssessmentNote({ assessment: null, unusable_files: 0 }), null);
  assert.equal(savedAssessmentNote({ assessment: saved(), unusable_files: 1 }), null);
  assert.doesNotMatch(SAVED_ASSESSMENT_UNAVAILABLE, /\d|reassessment-|\//);
});

test("the kicker shows the round, a readable saved date, and the file", () => {
  assert.equal(
    resultKicker(saved(), "en-GB", "UTC"),
    "Round 1 · saved 1 Jan 2026 · reassessment-20260101T000000Z-aaa111.json",
  );
  assert.equal(
    resultKicker(saved({ round: 2, created_at: undefined }), "en-GB", "UTC"),
    "Follow-up round · reassessment-20260101T000000Z-aaa111.json",
  );
  assert.equal(formatSavedDate("not a date"), null);
  assert.equal(formatSavedDate(null), null);
});

test("a saved result is outdated when answers changed since, or on this page", () => {
  assert.equal(isOutdated(saved(), false), false);
  assert.equal(outdatedNote(saved(), false), null);
  assert.equal(isOutdated(saved({ based_on_earlier_answers: true }), false), true);
  assert.match(outdatedNote(saved({ based_on_earlier_answers: true }), false), /^Based on earlier answers/);
  assert.match(outdatedNote(saved(), true), /^Based on earlier answers/);
  // A result produced on this page keeps its own wording.
  const fresh = { reassessment_id: "20260101T000000Z-aaa111", round: 1, report: report() };
  assert.equal(isOutdated(fresh, false), false);
  assert.match(outdatedNote(fresh, true), /^You've changed answers since this assessment/);
  assert.equal(isOutdated(null, true), false);
});

test("takeaways: structured, legacy prose, missing, or empty", () => {
  const item = { summary: "x", source_finding_ids: ["e1-f1"], question_ids: [] };
  assert.equal(takeawayKind(report({ takeaway: { blind_spot: "b" } })), "structured");
  assert.equal(takeawayKind(report({ takeaway: "Older prose." })), "legacy");
  assert.equal(takeawayKind(report({ unresolved_uncertainties: [item] })), "missing");
  assert.equal(takeawayKind(report()), "empty");
  assert.match(NO_TAKEAWAY_TEXT.missing, /No takeaway was recorded/);
});

test("a saved assessment from an earlier navigation is never applied", () => {
  const navigation = createNavigation();
  const forA = navigation.begin();
  navigation.begin(); // the user opened batch B
  const update = savedAssessmentUpdate(
    { assessment: saved({ batch_id: "clarify-a" }), unusable_files: 0 },
    { current: navigation.isCurrent(forA), assessing: false, shown: null, savedDuringRead: false },
  );
  assert.equal(update, null);
  assert.equal(forA.signal.aborted, true); // and its request was aborted
});

test("a saved assessment never replaces one started or shown on this page", () => {
  const latest = { assessment: saved(), unusable_files: 0 };
  const base = { current: true, assessing: false, shown: null, savedDuringRead: false };
  assert.equal(savedAssessmentUpdate(latest, { ...base, assessing: true }), null);
  assert.equal(savedAssessmentUpdate(latest, { ...base, shown: saved() }), null);
});

test("applying a saved assessment: shown, missing, unusable, failed, or saved-during-read", () => {
  const base = { current: true, assessing: false, shown: null, savedDuringRead: false };
  const found = saved();
  assert.deepEqual(savedAssessmentUpdate({ assessment: found, unusable_files: 1 }, base), {
    assessment: found,
    stale: false,
    note: null,
  });
  assert.deepEqual(savedAssessmentUpdate({ assessment: null, unusable_files: 0 }, base), {
    assessment: null,
    stale: false,
    note: null,
  });
  assert.equal(
    savedAssessmentUpdate({ assessment: null, unusable_files: 1 }, base).note,
    SAVED_ASSESSMENT_UNAVAILABLE,
  );
  assert.equal(savedAssessmentUpdate(null, base).note, SAVED_ASSESSMENT_UNAVAILABLE);
  assert.equal(
    savedAssessmentUpdate({ assessment: found, unusable_files: 0 }, { ...base, savedDuringRead: true })
      .stale,
    true,
  );
});

// --- Follow-up questions (fictional data) ---

const openItem = { summary: "x", source_finding_ids: ["e1-f1"], question_ids: [] };

test("follow-ups are offered only for a round-1 current assessment with open items", () => {
  const open = saved({ report: report({ unresolved_uncertainties: [openItem] }) });
  assert.deepEqual(followupAvailability(open, false), { offered: true, blocked: false });
  assert.deepEqual(followupAvailability(saved(), false), { offered: false, blocked: false });
  const round2 = { ...open, round: 2, conclusion_status: "review_conclusion" };
  assert.equal(followupAvailability(round2, false).offered, false);
  assert.equal(followupAvailability(null, false).offered, false);
  const remaining = saved({ report: report({ remaining_considerations: [openItem] }) });
  assert.equal(followupAvailability(remaining, false).offered, true);
});

test("follow-ups are blocked while the assessment is outdated", () => {
  const open = report({ unresolved_uncertainties: [openItem] });
  assert.deepEqual(
    followupAvailability(saved({ report: open, based_on_earlier_answers: true }), false),
    { offered: true, blocked: true },
  );
  assert.equal(followupAvailability(saved({ report: open }), true).blocked, true);
});

test("an assessment whose answers were edited while it ran needs reassessing", () => {
  // A fresh POST …/reassessments result: no created_at or based_on_earlier_answers.
  const fresh = {
    reassessment_id: "20260101T000000Z-aaa111",
    round: 1,
    conclusion_status: "current_assessment",
    report: report({ unresolved_uncertainties: [openItem] }),
    answers_changed_during_assessment: false,
  };
  assert.equal(needsReassessment(fresh, false), false);
  assert.deepEqual(followupAvailability(fresh, false), { offered: true, blocked: false });
  const edited = { ...fresh, answers_changed_during_assessment: true };
  assert.equal(needsReassessment(edited, false), true);
  assert.deepEqual(followupAvailability(edited, false), { offered: true, blocked: true });
  // The outdated note isn't repeated: the result already says answers were edited.
  assert.equal(outdatedNote(edited, false), null);
  assert.equal(needsReassessment(saved({ based_on_earlier_answers: true }), false), true);
  assert.equal(needsReassessment(saved(), true), true);
  assert.equal(needsReassessment(null, true), false);
});

test("the request names the assessment on screen and confirms the paid request", () => {
  assert.deepEqual(followupRequestBody(saved()), {
    reassessment_id: "20260101T000000Z-aaa111",
    confirm_paid_request: true,
  });
});

test("a follow-up with questions opens the new batch", () => {
  const view = {
    followup_batch_id: "clarify-r2",
    question_count: 2,
    no_useful_followup: false,
    open_concerns: true,
    reused: false,
    answers_changed_during_generation: false,
  };
  const outcome = followupOutcome(view);
  assert.equal(outcome.open, "clarify-r2");
  assert.match(outcome.announcement, /^2 follow-up questions ready/);
  assert.equal(outcome.notice, null);
  const edited = followupOutcome({ ...view, answers_changed_during_generation: true });
  assert.match(edited.notice, /edited outside this screen/);
  // Focus moves to the first question, so the announcement carries the notice too.
  assert.equal(
    edited.announcement,
    `2 follow-up questions ready. Round-1 answers are shown read-only below. ${edited.notice}`,
  );
  assert.doesNotMatch(outcome.announcement, /edited outside/);
  assert.match(followupOutcome({ ...view, question_count: 1 }).announcement, /^1 follow-up question ready/);
});

test("no useful follow-up stays on the assessment", () => {
  const view = { followup_batch_id: "clarify-r2", question_count: 0, no_useful_followup: true };
  const open = followupOutcome({ ...view, open_concerns: true });
  assert.equal(open.open, null);
  assert.match(open.message, /^No useful follow-up questions were found\. The assessment above/);
  assert.match(open.message, /doesn't mean the plan is risk-free/);
  assert.doesNotMatch(followupOutcome({ ...view, open_concerns: false }).message, /risk-free/);
});

test("follow-up errors: server text passes through; only 502 offers a paid retry", () => {
  assert.match(describeFollowupError({ network: true }).message, /outcome isn't known/);
  assert.equal(
    describeFollowupError({ status: 409, detail: "Assess your answers again first." }).message,
    "Assess your answers again first.",
  );
  assert.match(
    describeFollowupError({ status: 502, detail: "The model's response was cut off." }).message,
    /cut off\. You can try again \(another paid request\)\.$/,
  );
  const raw = describeFollowupError({ status: 500, detail: "<html>Traceback</html>" });
  assert.doesNotMatch(raw.message, /Traceback/);
});

test("follow-up errors: only a failed call, unknown outcome, or server error is retryable", () => {
  assert.equal(describeFollowupError({ network: true }).retryable, true);
  assert.equal(describeFollowupError({ status: 502, detail: "Cut off." }).retryable, true);
  assert.equal(describeFollowupError({ status: 500, detail: "<html>x</html>" }).retryable, true);
  assert.equal(describeFollowupError({ status: 418 }).retryable, true);
  for (const status of [403, 404, 409, 422, 503]) {
    assert.equal(describeFollowupError({ status, detail: "Refused." }).retryable, false, status);
  }
});

test("a follow-up response after navigation belongs to a stale ticket", () => {
  const navigation = createNavigation();
  navigation.begin(); // batch A shown
  const ticket = navigation.current(); // follow-up requested on A
  assert.equal(navigation.isCurrent(ticket), true);
  navigation.begin(); // the user opens another batch, or goes back
  assert.equal(navigation.isCurrent(ticket), false); // so the late result opens nothing
});
