// The answering screen: open a saved batch, show one question at a time, save each
// response, and (only when asked) assess the saved answers, through the HTTP API:
// GET /clarifications/{id}, PUT …/responses/{qid}, POST …/reassessments. Opening a batch
// also reads its latest saved assessment (GET …/assessments/latest), which never calls a
// model.
// All saved, model, and server text is inserted with textContent, never parsed as HTML.

import {
  assessmentEligibility,
  buildUpdate,
  conclusionHeading,
  createNavigation,
  describeAssessmentError,
  describeLoadError,
  describeSaveError,
  editableQuestions,
  initialText,
  isOutdated,
  isPlausibleBatchId,
  NO_TAKEAWAY_TEXT,
  nextQuestionId,
  outdatedNote,
  priorQuestions,
  progress,
  resultKicker,
  savedAssessmentUpdate,
  statusLabel,
  takeawayKind,
  takeawayStatusLabel,
} from "./state.mjs";

const $ = (id) => document.getElementById(id);

const views = ["open-view", "loading-view", "error-view", "batch-view"];

const state = {
  batchId: null,
  batch: null,
  currentId: null, // the question on the card; null shows the completion state
  saving: false,
  savingButton: null,
  assessing: false, // an assessment request from this page is pending
  assessment: null, // the result shown: produced on this page, or the latest saved one
  assessmentStale: false, // answers were changed on this page after that result
  assessmentError: null,
  savedNote: null, // why a saved assessment couldn't be shown, if one couldn't
  savesCompleted: 0, // answers saved on this page, to spot a save during a read
};

// Each load and save checks it still belongs to the current navigation before touching
// the screen, so a late response can never show one batch while saves go to another.
const navigation = createNavigation();

// Start a new navigation: abort a pending load, and stop waiting for a pending save or
// assessment (neither is cancelled, so it may still be stored, but its result is no
// longer shown). A result shown for the previous view is cleared too.
function navigate() {
  if (state.saving) setSaving(false);
  state.assessing = false;
  state.assessment = null;
  state.assessmentStale = false;
  state.assessmentError = null;
  state.savedNote = null;
  return navigation.begin();
}

// --- Network ---

class RequestFailure extends Error {
  constructor({ network = false, status = null, detail = null }) {
    super("request failed");
    this.failure = { network, status, detail };
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...(options.headers ?? {}) },
    });
  } catch {
    throw new RequestFailure({ network: true });
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null; // not JSON (for example a proxy error page): never shown raw
  }
  if (!response.ok) {
    throw new RequestFailure({ status: response.status, detail: body?.detail ?? null });
  }
  return body;
}

const batchPath = (batchId) => `/clarifications/${encodeURIComponent(batchId)}`;

// --- Views ---

function show(view) {
  for (const id of views) $(id).hidden = id !== view;
}

function announce(message) {
  // Clear first so the same message is announced again.
  $("announcer").textContent = "";
  window.setTimeout(() => {
    $("announcer").textContent = message;
  }, 50);
}

function setUrl(batchId, { push }) {
  const url = new URL(window.location.href);
  if (batchId) url.searchParams.set("batch", batchId);
  else url.searchParams.delete("batch");
  window.history[push ? "pushState" : "replaceState"]({}, "", url);
}

function showOpenView(prefill = "", error = null) {
  navigate();
  state.batchId = null;
  state.batch = null;
  show("open-view");
  const input = $("batch-id");
  input.value = prefill;
  setFieldError(input, $("batch-id-error"), error);
  input.focus();
}

function setFieldError(field, element, message) {
  element.textContent = message ?? "";
  element.hidden = !message;
  if (message) field.setAttribute("aria-invalid", "true");
  else field.removeAttribute("aria-invalid");
}

// --- Loading a batch ---

async function load(batchId) {
  const ticket = navigate();
  state.batchId = batchId;
  state.batch = null;
  show("loading-view");
  try {
    const batch = await request(batchPath(batchId), { signal: ticket.signal });
    if (!navigation.isCurrent(ticket)) return; // the user has moved on
    state.batch = batch;
    state.currentId = batch.next_question_id;
    render();
    show("batch-view");
    focusHeading();
  } catch (error) {
    if (!navigation.isCurrent(ticket)) return; // includes the abort itself
    if (!(error instanceof RequestFailure)) throw error;
    const { title, message, retry } = describeLoadError(error.failure);
    $("error-title").textContent = title;
    $("error-message").textContent = message;
    $("retry-load").hidden = !retry;
    show("error-view");
    $("error-title").focus();
    return;
  }
  loadSavedAssessment(batchId, ticket);
}

// The latest saved assessment, read after the questions are shown so answering never waits
// for it. A read only: it never starts an assessment. It arrives quietly (no focus change
// or announcement), and only into the navigation it was requested for.
async function loadSavedAssessment(batchId, ticket) {
  const savesBefore = state.savesCompleted;
  let latest = null;
  try {
    latest = await request(`${batchPath(batchId)}/assessments/latest`, { signal: ticket.signal });
  } catch (error) {
    if (!navigation.isCurrent(ticket)) return;
    if (!(error instanceof RequestFailure)) throw error;
  }
  const update = savedAssessmentUpdate(latest, {
    current: navigation.isCurrent(ticket),
    assessing: state.assessing,
    shown: state.assessment,
    savedDuringRead: state.savesCompleted !== savesBefore,
  });
  if (update === null) return;
  state.assessment = update.assessment;
  state.assessmentStale = update.stale;
  state.savedNote = update.note;
  renderAssessmentParts();
}

// --- Rendering ---

function render() {
  const batch = state.batch;
  const question = editableQuestions(batch).find((q) => q.id === state.currentId) ?? null;
  if (question === null) state.currentId = null;

  renderProgress(batch, question);
  $("plan-text").textContent = batch.plan;

  $("question-card").hidden = question === null;
  $("done-card").hidden = question !== null;
  if (question !== null) renderCard(question);
  else renderDone(batch);

  renderAssess(batch, question);
  renderResult();
  renderSavedNote();

  renderList($("review-list"), editableQuestions(batch), { editable: true });
  const prior = priorQuestions(batch);
  $("prior").hidden = prior.length === 0;
  renderList($("prior-list"), prior, { editable: false });
  applyBusy();
}

function renderProgress(batch, question) {
  const { total, responded } = progress(batch);
  const bar = $("progress-bar");
  bar.max = Math.max(total, 1);
  bar.value = responded;
  const round = batch.round === 2 ? "Follow-up round · " : "";
  const position =
    question === null
      ? ""
      : `Question ${editableQuestions(batch).indexOf(question) + 1} of ${total} · `;
  $("progress-text").textContent = `${round}${position}${responded} of ${total} with a response`;
}

function renderCard(question) {
  const list = $("perspectives");
  list.replaceChildren(
    ...(question.perspectives.length ? question.perspectives : ["Unknown"]).map((name) => {
      const item = document.createElement("li");
      item.textContent = name;
      return item;
    }),
  );
  $("question-text").textContent = question.question;
  $("why-text").textContent = question.why_it_matters;

  const current = $("current-status");
  current.hidden = question.status === null;
  current.replaceChildren();
  if (question.status !== null) {
    const badge = statusBadge(question.status);
    current.append("Current response: ", badge);
    if (question.status === "deferred") {
      current.append(" — choose a response below, or keep it for later.");
    }
  }

  $("answer-text").value = initialText(question);
  setFieldError($("answer-text"), $("answer-error"), null);
  $("save-error").hidden = true;
  $("save-error").textContent = "";
}

function renderDone(batch) {
  const { total, responded, deferred } = progress(batch);
  $("done-title").textContent =
    total === 0
      ? "This batch has no questions to answer"
      : "Every question in this round has a response";
  $("done-summary").textContent =
    total === 0
      ? "There is nothing to save here."
      : `${responded} of ${total} questions have a response. You can review or change them below.`;
  const deferredNote = $("done-deferred");
  deferredNote.hidden = deferred === 0;
  deferredNote.textContent =
    deferred === 1
      ? "1 question is saved for later and still needs an answer when you're ready."
      : `${deferred} questions are saved for later and still need an answer when you're ready.`;
  $("review").open = total > 0;
}

function statusBadge(status) {
  const badge = document.createElement("span");
  badge.className = "status-badge";
  badge.dataset.status = status ?? "none";
  badge.textContent = statusLabel(status);
  return badge;
}

function renderList(list, questions, { editable }) {
  const template = $("answer-item");
  list.replaceChildren(
    ...questions.map((q, index) => {
      const item = template.content.firstElementChild.cloneNode(true);
      item.querySelector(".answer-question").textContent = q.question;
      item.querySelector(".status-badge").replaceWith(statusBadge(q.status));
      const text = item.querySelector(".answer-text");
      text.hidden = !q.answer;
      text.textContent = q.answer ?? "";
      const button = item.querySelector("button");
      if (editable) {
        const verb = q.status === null ? "Answer" : q.status === "deferred" ? "Revisit" : "Edit";
        button.hidden = false;
        button.textContent = verb;
        const hidden = document.createElement("span");
        hidden.className = "visually-hidden";
        hidden.textContent = ` question ${index + 1}`;
        button.append(hidden);
        button.disabled = busy();
        button.addEventListener("click", () => openQuestion(q.id));
      } else {
        button.remove();
      }
      return item;
    }),
  );
}

// --- Assessment ---

// Re-render only the assessment cards, leaving the question card (and any typed text) alone.
function renderAssessmentParts() {
  if (state.batch === null) return;
  const question =
    editableQuestions(state.batch).find((q) => q.id === state.currentId) ?? null;
  renderAssess(state.batch, question);
  renderResult();
  renderSavedNote();
  applyBusy();
}

function renderSavedNote() {
  $("saved-assessment-note").hidden = !state.savedNote;
  $("saved-assessment-note").textContent = state.savedNote ?? "";
}

function renderAssess(batch, question) {
  const card = $("assess-card");
  const { total } = progress(batch);
  card.hidden = question !== null || total === 0;
  if (card.hidden) return;

  const { eligible, deferred, reason } = assessmentEligibility(batch);
  const deferredNote = $("assess-deferred");
  deferredNote.hidden = !eligible || deferred === 0;
  deferredNote.textContent =
    deferred === 1
      ? "1 question is saved for later and hasn't been answered. The assessment treats it " +
        "as open, not as answered."
      : `${deferred} questions are saved for later and haven't been answered. The ` +
        "assessment treats them as open, not as answered.";
  $("assess-ineligible").hidden = eligible;
  $("assess-ineligible").textContent = reason ?? "";

  // Started elsewhere (another tab, or before a reload): this page can't show its result
  // live; it appears after a reload once the assessment finishes.
  const elsewhere = batch.assessment_running && !state.assessing;
  $("assess-elsewhere").hidden = !elsewhere;
  $("assess-action").hidden = !eligible || elsewhere;

  $("assess-button").textContent = state.assessing
    ? "Assessing…"
    : state.assessmentError
      ? "Try again"
      : state.assessment
        ? "Assess again"
        : "Assess my answers";
  // After a current result, assessing again is a secondary choice (another paid request).
  const secondary =
    state.assessment !== null && !isOutdated(state.assessment, state.assessmentStale) &&
    !state.assessing;
  $("assess-button").classList.toggle("primary", !secondary);
  $("assess-button").classList.toggle("secondary", secondary);
  $("assess-waiting").hidden = !state.assessing;
  $("assess-error").hidden = !state.assessmentError;
  $("assess-error").textContent = state.assessmentError ?? "";
}

function renderResult() {
  const result = state.assessment;
  $("result-card").hidden = result === null;
  if (result === null) return;
  const report = result.report;

  $("result-kicker").textContent = resultKicker(result);
  $("result-title").textContent = conclusionHeading(result.conclusion_status);

  const notes = [];
  if (result.answers_changed_during_assessment) {
    notes.push(
      "Your saved answers were edited outside this screen while the assessment ran. This " +
        "result reflects the answers it used, not the newer ones.",
    );
  }
  const outdated = outdatedNote(result, state.assessmentStale);
  if (outdated) notes.push(outdated);
  $("result-changed").hidden = notes.length === 0;
  $("result-changed").textContent = notes.join(" ");

  const takeaway = report.takeaway;
  const kind = takeawayKind(report);
  $("takeaway").hidden = kind !== "structured";
  $("takeaway-legacy").hidden = kind !== "legacy";
  $("takeaway-legacy-text").textContent = kind === "legacy" ? takeaway : "";
  $("takeaway-none").hidden = !(kind in NO_TAKEAWAY_TEXT);
  $("takeaway-none").textContent = NO_TAKEAWAY_TEXT[kind] ?? "";
  if (kind === "structured") {
    $("takeaway-blind-spot").textContent = takeaway.blind_spot;
    $("takeaway-why").textContent = takeaway.why_it_mattered;
    const badge = $("takeaway-status");
    badge.dataset.status = takeaway.status;
    badge.textContent = takeawayStatusLabel(takeaway.status);
    $("takeaway-explanation").textContent = takeaway.status_explanation;
  }
  $("closing-note").hidden = !result.closing_note;
  $("closing-note").textContent = result.closing_note ?? "";

  renderSection("remaining", "Remaining concerns", report.remaining_considerations, (item) => [
    textElement("p", item.title, "item-title"),
    textElement("p", item.observation),
    textElement("p", `Why it matters: ${item.why_it_matters}`, "item-why"),
  ]);
  renderSection("addressed", "Addressed concerns", report.addressed_concerns, (item) => [
    textElement("p", item.summary),
  ]);
  renderSection(
    "unresolved",
    "Unresolved uncertainties",
    report.unresolved_uncertainties,
    (item) => [textElement("p", item.summary)],
  );

  $("result-next").textContent =
    result.round === 1
      ? "This is the current assessment after round 1. One follow-up round can be generated " +
        "from the command line (one more paid request); this screen doesn't start it."
      : "This is the conclusion of the review; Blindspot supports only one follow-up round.";
}

function textElement(tag, text, className = null) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function renderSection(name, label, items, build) {
  $(`${name}-summary`).textContent = `${label} (${items.length})`;
  $(`${name}-list`).replaceChildren(
    ...(items.length
      ? items.map((item) => {
          const li = document.createElement("li");
          li.append(...build(item));
          return li;
        })
      : [textElement("li", "None.", "empty")]),
  );
}

async function assess() {
  if (busy() || state.batch === null) return;
  const ticket = navigation.current();
  state.assessing = true;
  state.assessmentError = null;
  render();
  announce("Assessing your answers. This can take up to a minute.");
  try {
    const result = await request(`${batchPath(state.batchId)}/reassessments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_paid_request: true }),
    });
    if (!navigation.isCurrent(ticket)) return; // another batch is shown now
    state.assessing = false;
    state.assessment = result;
    state.assessmentStale = false;
    state.savedNote = null;
    render();
    announce(`${conclusionHeading(result.conclusion_status)} is ready.`);
    $("result-title").focus();
  } catch (error) {
    if (!navigation.isCurrent(ticket)) return;
    state.assessing = false;
    if (!(error instanceof RequestFailure)) throw error;
    state.assessmentError = describeAssessmentError(error.failure);
    render();
  }
}

function focusHeading() {
  const heading = state.currentId === null ? $("done-title") : $("question-text");
  heading.focus();
}

function openQuestion(questionId) {
  if (busy()) return;
  state.currentId = questionId;
  render();
  $("question-card").scrollIntoView({ block: "start" });
  focusHeading();
}

// --- Saving ---

// While saving or assessing, every action is disabled and the text box is read-only, so
// nothing typed after clicking can be lost, and answers can't change mid-assessment.
function busy() {
  return state.saving || state.assessing;
}

function applyBusy() {
  const disabled = busy();
  $("answer-form").setAttribute("aria-busy", String(state.saving));
  $("assess-card").setAttribute("aria-busy", String(state.assessing));
  $("answer-text").readOnly = disabled;
  for (const button of document.querySelectorAll(
    "#answer-form button, #review-list button, #assess-button",
  )) {
    button.disabled = disabled;
  }
}

function setSaving(saving, activeButton = null) {
  state.saving = saving;
  applyBusy();
  const restore = state.savingButton;
  if (restore?.dataset.label) {
    restore.textContent = restore.dataset.label;
    delete restore.dataset.label;
  }
  state.savingButton = saving ? activeButton : null;
  if (saving && activeButton) {
    activeButton.dataset.label = activeButton.textContent;
    activeButton.textContent = "Saving…";
  }
}

async function save(action, button) {
  if (busy() || state.currentId === null) return;
  const textarea = $("answer-text");
  const saveError = $("save-error");
  saveError.hidden = true;

  const update = buildUpdate(action, textarea.value);
  if (update.error) {
    setFieldError(textarea, $("answer-error"), update.error);
    textarea.focus();
    return;
  }
  setFieldError(textarea, $("answer-error"), null);

  // The save belongs to the batch and navigation it started in.
  const ticket = navigation.current();
  const path = `${batchPath(state.batchId)}/responses/${encodeURIComponent(state.currentId)}`;
  const questionId = state.currentId;
  setSaving(true, button);
  announce("Saving…");
  try {
    const batch = await request(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update.body),
    });
    if (!navigation.isCurrent(ticket)) return; // the user has moved on; show nothing
    setSaving(false);
    state.batch = batch;
    state.savesCompleted += 1;
    if (state.assessment) state.assessmentStale = true;
    state.currentId = nextQuestionId(batch, questionId);
    render();
    announce(`Saved: ${statusLabel(update.body.status)}.`);
    focusHeading();
  } catch (error) {
    if (!navigation.isCurrent(ticket)) return;
    setSaving(false);
    if (!(error instanceof RequestFailure)) throw error;
    // The typed text is left exactly as it was.
    saveError.textContent = describeSaveError(error.failure);
    saveError.hidden = false;
  }
}

// --- Wiring ---

function start() {
  $("open-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("batch-id").value.trim();
    if (!isPlausibleBatchId(value)) {
      setFieldError(
        $("batch-id"),
        $("batch-id-error"),
        "Enter a batch ID that starts with clarify-, using only letters, numbers, " +
          "hyphens, and underscores.",
      );
      $("batch-id").focus();
      return;
    }
    setUrl(value, { push: true });
    load(value);
  });

  $("answer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    save("answered", event.submitter ?? document.querySelector('[data-action="answered"]'));
  });
  for (const button of document.querySelectorAll('#answer-form button[type="button"]')) {
    button.addEventListener("click", () => save(button.dataset.action, button));
  }

  $("assess-button").addEventListener("click", assess);
  $("retry-load").addEventListener("click", () => load(state.batchId));
  for (const button of document.querySelectorAll("[data-open-another]")) {
    button.addEventListener("click", () => {
      setUrl(null, { push: true });
      showOpenView();
    });
  }

  window.addEventListener("popstate", route);
  route();
}

function route() {
  const batchId = new URL(window.location.href).searchParams.get("batch")?.trim() ?? "";
  if (!batchId) showOpenView();
  else if (!isPlausibleBatchId(batchId)) {
    showOpenView(batchId, "That doesn't look like a batch ID. Batch IDs start with clarify-.");
  } else load(batchId);
}

start();
