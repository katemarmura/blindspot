// Pure helpers for the answering screen: no DOM, no network. Tested with `node --test`.
//
// The batch object is the API's BatchView (GET /clarifications/{batch_id}).
// A question's `status` is "answered", "dont_know", "not_applicable", "deferred",
// or null (not answered yet). Only `editable` questions belong to this round.

export const STATUS_LABELS = Object.freeze({
  answered: "Answered",
  dont_know: "I don't know",
  not_applicable: "Not applicable",
  deferred: "Saved for later",
});

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? "Not answered yet";
}

export function editableQuestions(batch) {
  return batch.questions.filter((q) => q.editable);
}

export function priorQuestions(batch) {
  return batch.questions.filter((q) => !q.editable);
}

// The next unanswered question of this round: the first after `afterId` in order,
// then from the start. Deferred questions have a response, so they're not "next";
// they stay reachable through review. Returns null when nothing is unanswered.
export function nextQuestionId(batch, afterId = null) {
  const questions = editableQuestions(batch);
  const start = questions.findIndex((q) => q.id === afterId) + 1;
  const ordered = [...questions.slice(start), ...questions.slice(0, start)];
  return ordered.find((q) => q.status === null)?.id ?? null;
}

export function progress(batch) {
  const questions = editableQuestions(batch);
  const count = (predicate) => questions.filter(predicate).length;
  return {
    total: questions.length,
    responded: count((q) => q.status !== null),
    deferred: count((q) => q.status === "deferred"),
    unanswered: count((q) => q.status === null),
  };
}

// The PUT body for an action, or an error message to show instead of saving.
// "answer" needs text; "not_applicable" keeps optional text as its explanation;
// "dont_know" and "deferred" never send text.
export function buildUpdate(action, text) {
  const trimmed = (text ?? "").trim();
  switch (action) {
    case "answered":
      if (!trimmed) {
        return { error: "Type an answer first, or choose one of the other options." };
      }
      return { body: { status: "answered", answer: trimmed } };
    case "not_applicable":
      return { body: { status: "not_applicable", answer: trimmed || null } };
    case "dont_know":
    case "deferred":
      return { body: { status: action, answer: null } };
    default:
      return { error: "Unknown action." };
  }
}

// The text box starts with the saved text for answers and explanations only.
export function initialText(question) {
  return question.status === "answered" || question.status === "not_applicable"
    ? (question.answer ?? "")
    : "";
}

// The API's error `detail`: a plain message string, or a list of validation errors.
function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((d) => d?.msg).filter((m) => typeof m === "string");
    if (messages.length) return messages.join("; ");
  }
  return null;
}

// What to show when loading a batch fails. `failure` is {network: true} or
// {status, detail}. Returns {title, message, retry}.
export function describeLoadError(failure) {
  if (failure.network) {
    return {
      title: "Can't reach Blindspot",
      message: "The server didn't respond. Check that it's running, then try again.",
      retry: true,
    };
  }
  const detail = detailText(failure.detail);
  if (failure.status === 404) {
    return {
      title: "Batch not found",
      message: "No saved batch has that ID. Check the ID (it starts with clarify-) and try again.",
      retry: false,
    };
  }
  if (failure.status === 403 || failure.status === 409) {
    return {
      title: "This batch can't be opened",
      message: detail ?? "Its saved files are invalid. Fix them, then reload.",
      retry: true,
    };
  }
  return {
    title: "Something went wrong",
    message: "The server couldn't load this batch. Try again in a moment.",
    retry: true,
  };
}

// What to show when saving fails; the typed text is always kept.
export function describeSaveError(failure) {
  const keep = "Your text is still here; nothing was lost.";
  if (failure.network) {
    return `Couldn't save: the server didn't respond. ${keep} Try again when it's back.`;
  }
  const detail = detailText(failure.detail);
  if (failure.status === 422 && detail) {
    return `Couldn't save: ${detail}. ${keep}`;
  }
  if ((failure.status === 403 || failure.status === 404 || failure.status === 409) && detail) {
    return `Couldn't save: ${detail} ${keep}`;
  }
  return `Couldn't save because of a server error. ${keep} Try again.`;
}

// --- Assessment ---

// Mirrors the server's rule: an assessment needs at least one response in this round
// that isn't "saved for later" ("I don't know" and "not applicable" count).
export function assessmentEligibility(batch) {
  const questions = editableQuestions(batch);
  const deferred = questions.filter((q) => q.status === "deferred").length;
  const usable = questions.filter((q) => q.status !== null && q.status !== "deferred").length;
  if (questions.length === 0) {
    return { eligible: false, deferred, reason: "This batch has no questions to assess." };
  }
  if (usable === 0) {
    return {
      eligible: false,
      deferred,
      reason:
        "There's nothing to assess yet: every question is saved for later. Answer at least " +
        "one (“I don't know” and “Not applicable” count) to get an assessment.",
    };
  }
  return { eligible: true, deferred, reason: null };
}

export function conclusionHeading(conclusionStatus) {
  return conclusionStatus === "review_conclusion" ? "Conclusion of this review" : "Current assessment";
}

export const TAKEAWAY_STATUS_LABELS = Object.freeze({
  open: "Open",
  partly_addressed: "Partly addressed",
  addressed: "Addressed",
});

export function takeawayStatusLabel(status) {
  return TAKEAWAY_STATUS_LABELS[status] ?? "Unknown";
}

// What to show when an assessment request fails. The server's messages for these
// statuses are written for people and contain no model output.
export function describeAssessmentError(failure) {
  if (failure.network) {
    return (
      "The server didn't respond, so the outcome isn't known: the assessment may still " +
      "have run, and any result is saved in the batch folder. Trying again makes another " +
      "paid request."
    );
  }
  const detail = detailText(failure.detail);
  if (detail && [403, 404, 409, 422, 502, 503].includes(failure.status)) return detail;
  return (
    "The assessment couldn't be completed because of a server error. Your answers are " +
    "still saved. Trying again makes another paid request."
  );
}

// Guards against late responses. Every navigation (opening a batch, going back or
// forward, retrying, returning to the form) starts a new generation; a request remembers
// the generation it started in, and its result is applied only if that is still current.
// `begin` also aborts the previous generation's loads. Saves are never aborted (a cancelled
// write might still have been stored); their late results are just ignored.
export function createNavigation() {
  let generation = 0;
  let controller = null;
  return {
    begin() {
      controller?.abort();
      controller = new AbortController();
      generation += 1;
      return { id: generation, signal: controller.signal };
    },
    // A ticket for work that belongs to the current navigation without starting a new
    // one (a save); it has no signal, because saves are never aborted.
    current() {
      return { id: generation, signal: null };
    },
    isCurrent(ticket) {
      return ticket.id === generation;
    },
  };
}

// Batch IDs are folder names like clarify-20261009T140042Z-6b0833 (mirrors the API).
export function isPlausibleBatchId(value) {
  return /^clarify-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value);
}

// --- Reopening a saved assessment ---
//
// GET …/assessments/latest returns {assessment, unusable_files}. A reopened assessment
// (SavedAssessmentView) has `created_at` and `based_on_earlier_answers`; one produced on
// this page (AssessmentView) has neither.

export const SAVED_ASSESSMENT_UNAVAILABLE =
  "A saved assessment couldn't be shown. You can keep answering; nothing was lost.";

// What to show when there is no assessment to display: the neutral note when loading
// failed or every saved file was unusable, otherwise nothing. Never a count or a path.
export function savedAssessmentNote(latest) {
  if (latest === null) return SAVED_ASSESSMENT_UNAVAILABLE; // the request failed
  if (latest.assessment === null && latest.unusable_files > 0) return SAVED_ASSESSMENT_UNAVAILABLE;
  return null;
}

// The saved date in the viewer's locale, or null when it is missing or unreadable.
export function formatSavedDate(createdAt, locale = undefined, timeZone = undefined) {
  if (typeof createdAt !== "string") return null;
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone }).format(date);
}

export function resultKicker(result, locale = undefined, timeZone = undefined) {
  const parts = [result.round === 2 ? "Follow-up round" : "Round 1"];
  const saved = formatSavedDate(result.created_at, locale, timeZone);
  if (saved) parts.push(`saved ${saved}`);
  parts.push(`reassessment-${result.reassessment_id}.json`);
  return parts.join(" · ");
}

// The result no longer reflects the saved answers: it was saved before they changed, or
// answers were changed on this page after it was shown.
export function isOutdated(result, changedOnPage) {
  return result !== null && (changedOnPage || result.based_on_earlier_answers === true);
}

// Whether the result should be assessed again before anything builds on it: it is
// outdated, or answers were edited outside this screen while it ran (the server compares
// the saved answers and refuses a follow-up either way). Both the follow-up block and the
// primary "Assess again" button use this.
export function needsReassessment(result, changedOnPage) {
  return (
    isOutdated(result, changedOnPage) || result?.answers_changed_during_assessment === true
  );
}

export function outdatedNote(result, changedOnPage) {
  if (!isOutdated(result, changedOnPage)) return null;
  if (result.created_at !== undefined) {
    return (
      "Based on earlier answers. Some saved answers have changed since this assessment " +
      "was saved, so it may not reflect them."
    );
  }
  return (
    "You've changed answers since this assessment. It reflects the answers it used; " +
    "assess again to include your changes."
  );
}

// How to show a report's takeaway. Older saved reports may hold prose ("legacy") or no
// takeaway at all ("missing"); new reports have none only when no items are listed.
export function takeawayKind(report) {
  const takeaway = report.takeaway;
  if (typeof takeaway === "string") return "legacy";
  if (takeaway !== null && typeof takeaway === "object") return "structured";
  const items =
    report.remaining_considerations.length +
    report.addressed_concerns.length +
    report.unresolved_uncertainties.length;
  return items > 0 ? "missing" : "empty";
}

export const NO_TAKEAWAY_TEXT = Object.freeze({
  empty: "This assessment has no takeaway because it lists no concerns or uncertainties.",
  missing: "No takeaway was recorded; read the detailed sections.",
});

// What a finished read of the saved assessment changes on screen, or null to change
// nothing: the read belongs to a navigation that is no longer current (another batch is
// shown), or an assessment started on this page meanwhile (it is newer). `latest` is the
// response, or null when the request failed.
export function savedAssessmentUpdate(latest, { current, assessing, shown, savedDuringRead }) {
  if (!current || assessing || shown !== null) return null;
  const assessment = latest?.assessment ?? null;
  return {
    assessment,
    // An answer saved during the read may not be reflected in the server's comparison.
    stale: assessment !== null && savedDuringRead,
    note: assessment === null ? savedAssessmentNote(latest) : null,
  };
}

// --- Follow-up questions (round 2) ---

function hasOpenItems(report) {
  return report.remaining_considerations.length + report.unresolved_uncertainties.length > 0;
}

// Whether to offer "Ask follow-up questions" for the result on screen: only a round-1
// current assessment that still lists something open. When answers changed since it (or
// while it ran), the action is blocked until the user assesses again (the server refuses
// it too).
export function followupAvailability(result, changedOnPage) {
  if (
    result === null ||
    result.round !== 1 ||
    result.conclusion_status !== "current_assessment" ||
    !hasOpenItems(result.report)
  ) {
    return { offered: false, blocked: false };
  }
  return { offered: true, blocked: needsReassessment(result, changedOnPage) };
}

// The POST body: the exact assessment on screen, with the explicit paid-request flag.
export function followupRequestBody(result) {
  return { reassessment_id: result.reassessment_id, confirm_paid_request: true };
}

// What a successful response means for the screen: open the new round-2 batch, or stay
// and show that there is no useful follow-up.
export function followupOutcome(view) {
  if (view.no_useful_followup) {
    return {
      open: null,
      message:
        "No useful follow-up questions were found. The assessment above is the conclusion " +
        "available for this session." +
        (view.open_concerns
          ? " Some concerns are still open; this doesn't mean the plan is risk-free."
          : ""),
    };
  }
  const count = view.question_count;
  const notice = view.answers_changed_during_generation
    ? "Your round-1 answers were edited outside this screen while the follow-up questions " +
      "were generated. The follow-up uses the answers that were assessed, shown below as " +
      "earlier answers."
    : null;
  const ready =
    `${count} follow-up ${count === 1 ? "question" : "questions"} ready. Round-1 answers ` +
    "are shown read-only below.";
  return {
    open: view.followup_batch_id,
    // Focus moves to the first question, so the notice is announced as well as shown.
    announcement: notice ? `${ready} ${notice}` : ready,
    notice,
  };
}

// What to show when a follow-up request fails, and whether trying again can help (the
// button then reads "Try again"). Only a failed model call (502), an unknown outcome
// (network), or an unexpected server error can succeed on a retry; the other statuses
// (stale answers, invalid lineage, round 2, already running, not configured) need
// something else to change first. Server messages for the known statuses are written for
// people and contain no model output.
export function describeFollowupError(failure) {
  if (failure.network) {
    return {
      message:
        "The server didn't respond, so the outcome isn't known: follow-up questions may " +
        "still have been generated. Once it has finished, pressing the button opens saved " +
        "questions at no charge; if none were saved, it makes a new paid request.",
      retryable: true,
    };
  }
  const detail = detailText(failure.detail);
  if (detail && failure.status === 502) {
    return { message: `${detail} You can try again (another paid request).`, retryable: true };
  }
  if (detail && [403, 404, 409, 422, 503].includes(failure.status)) {
    return { message: detail, retryable: false };
  }
  return {
    message:
      "Follow-up questions couldn't be generated because of a server error. Nothing was " +
      "changed. You can try again (another paid request).",
    retryable: true,
  };
}
