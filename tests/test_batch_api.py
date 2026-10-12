"""The HTTP interface for viewing, answering, and assessing a saved clarification batch.

Batches are copies of the committed kitten fixture in a temporary runs directory; a
round-2 batch is assembled from it by hand. Viewing and answering use no model or
credentials. Assessment tests replace the Anthropic client with a fake and set a dummy key;
nothing reaches the network.
"""

import asyncio
import hashlib
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anthropic
import pytest
from anthropic.types import Message, StopReason, TextBlock, Usage
from fastapi.testclient import TestClient

from blindspot import clarification
from blindspot.main import create_app

_EVALS = Path(__file__).resolve().parents[1] / "evals"
_ROUND_1_FIXTURE = _EVALS / "reassessment" / "kitten-before-holiday" / "clarification.json"
_REASSESSMENT_FIXTURE = (
    _EVALS / "followup" / "kitten-after-first-round" / "reassessment-20260101T000000Z-fixture.json"
)
_RUN = "team-20260101T000000Z-abc123"
_ROUND_1 = "clarify-20260101T000100Z-aaa111"
_ROUND_2 = "clarify-20260101T000200Z-bbb222"


@pytest.fixture(autouse=True)
def _no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No .env in the working directory and no key in the environment: nothing here needs one.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def client(runs: Path) -> Iterator[TestClient]:
    with TestClient(create_app(runs)) as test_client:
        yield test_client


def _round_1(runs: Path, name: str = _ROUND_1, parent: str = _RUN) -> Path:
    directory = runs / parent / name
    directory.mkdir(parents=True)
    shutil.copy(_ROUND_1_FIXTURE, directory / "clarification.json")
    return directory


def _round_2(runs: Path, prior_research: dict[str, Any] | None = None) -> Path:
    """A follow-up batch (q4) after the fixture's round-1 reassessment."""
    round_1 = _round_1(runs)
    data = json.loads((round_1 / "clarification.json").read_text())
    reassessment = json.loads(_REASSESSMENT_FIXTURE.read_text())
    data.update(
        clarification_id=_ROUND_2,
        round=2,
        previous={
            "clarification_id": data["clarification_id"],
            "clarification_file": str(round_1 / "clarification.json"),
            "reassessment_id": reassessment["reassessment_id"],
            "reassessment_file": str(round_1 / "reassessment.json"),
        },
        prior_questions=data["questions"],
        prior_responses=reassessment["recorded_responses"],
        prior_unanswered_question_ids=reassessment["unanswered_question_ids"],
        prior_reassessment=reassessment["report"],
        questions=[
            {
                "id": "q4",
                "question": "How old will the kitten be on departure day?",
                "why_it_matters": "It decides whether a vaccination falls during the trip.",
                "source_finding_ids": ["e3-f1"],
            }
        ],
    )
    if prior_research is not None:
        data["prior_research"] = prior_research
    directory = runs / _RUN / _ROUND_2
    directory.mkdir()
    (directory / "clarification.json").write_text(json.dumps(data))
    clarification.load_artifact(directory / "clarification.json")  # a valid round-2 batch
    return directory


def _responses(directory: Path) -> dict[str, tuple[Any, Any]]:
    data = json.loads((directory / "responses.json").read_text())
    return {e["question_id"]: (e["status"], e["answer"]) for e in data["responses"]}


def _digest(directory: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


def _statuses(body: dict[str, Any]) -> dict[str, Any]:
    return {q["id"]: q["status"] for q in body["questions"]}


# --- Reading ---


def test_reads_a_round_1_batch_with_perspectives(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)

    response = client.get(f"/clarifications/{_ROUND_1}")

    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == _ROUND_1
    assert body["round"] == 1
    assert body["responses_saved"] is False
    assert body["next_question_id"] == "q1"
    q1, q2, q3 = body["questions"]
    assert q1["question"] == "How old is the kitten, and which vaccinations has it had?"
    assert q1["why_it_matters"].startswith("The answer affects how much daily care")
    # Derived from the cited findings, in team order.
    assert q1["perspectives"] == ["Operations & Logistics", "Health & Safety"]
    assert q2["perspectives"] == ["Human & Behavioral"]
    assert q3["source_finding_ids"] == ["e2-f2", "e3-f2"]
    assert all(q["status"] is None and q["answer"] is None for q in body["questions"])
    assert all(q["round"] == 1 and q["editable"] for q in body["questions"])
    assert not (directory / "responses.json").exists()  # reading never creates files


def test_reads_a_round_2_batch_with_fixed_earlier_answers(runs: Path, client: TestClient) -> None:
    _round_2(runs)

    body = client.get(f"/clarifications/{_ROUND_2}").json()

    assert body["round"] == 2
    assert [(q["id"], q["round"], q["editable"]) for q in body["questions"]] == [
        ("q1", 1, False),
        ("q2", 1, False),
        ("q3", 1, False),
        ("q4", 2, True),
    ]
    assert _statuses(body) == {"q1": "answered", "q2": "dont_know", "q3": "deferred", "q4": None}
    assert body["questions"][0]["answer"].startswith("The kitten is 1.5 months old.")
    assert body["questions"][3]["perspectives"] == ["Health & Safety"]
    assert body["next_question_id"] == "q4"


# --- Saving and resuming ---


def test_saves_answers_and_resumes_after_reload(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    artifact_before = _digest(directory)

    first = client.put(
        f"/clarifications/{_ROUND_1}/responses/q1",
        json={"status": "answered", "answer": "Ten weeks; first vaccination done."},
    )
    second = client.put(f"/clarifications/{_ROUND_1}/responses/q2", json={"status": "dont_know"})

    assert first.status_code == second.status_code == 200
    assert second.json()["next_question_id"] == "q3"
    # A fresh app (as after a restart) sees the same saved state.
    with TestClient(create_app(runs)) as fresh:
        body = fresh.get(f"/clarifications/{_ROUND_1}").json()
    assert body["responses_saved"] is True
    assert _statuses(body) == {"q1": "answered", "q2": "dont_know", "q3": None}
    assert body["next_question_id"] == "q3"
    # "I don't know" is recorded; q3 is simply unanswered.
    assert _responses(directory) == {
        "q1": ("answered", "Ten weeks; first vaccination done."),
        "q2": ("dont_know", None),
        "q3": (None, None),
    }
    assert _digest(directory)["clarification.json"] == artifact_before["clarification.json"]
    # The CLI reads the same file.
    artifact = clarification.load_artifact(directory / "clarification.json")
    recorded = clarification.load_responses(directory / "responses.json", artifact)
    assert [(r.question_id, r.status) for r in recorded.recorded] == [
        ("q1", "answered"),
        ("q2", "dont_know"),
    ]
    assert recorded.unanswered == ["q3"]


def test_update_changes_only_the_selected_response(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    artifact = json.loads((directory / "clarification.json").read_text())
    original = {
        "clarification_id": artifact["clarification_id"],
        "instructions": "My own note.",
        "responses": [
            {"question_id": "q2", "status": "deferred", "answer": None, "question": "Ref."},
            {"question_id": "q1", "status": "answered", "answer": "First answer."},
        ],
    }
    (directory / "responses.json").write_text(json.dumps(original))

    response = client.put(
        f"/clarifications/{_ROUND_1}/responses/q1",
        json={"status": "not_applicable", "answer": "The kitten stays with family."},
    )

    assert response.status_code == 200
    saved = json.loads((directory / "responses.json").read_text())
    assert saved["instructions"] == "My own note."
    assert saved["responses"][0] == original["responses"][0]
    assert saved["responses"][1] == {
        "question_id": "q1",
        "status": "not_applicable",
        "answer": "The kitten stays with family.",
    }

    cleared = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": None})
    assert _statuses(cleared.json()) == {"q1": None, "q2": "deferred", "q3": None}
    assert cleared.json()["next_question_id"] == "q1"


def test_round_2_answers_only_this_round(runs: Path, client: TestClient) -> None:
    directory = _round_2(runs)

    earlier = client.put(
        f"/clarifications/{_ROUND_2}/responses/q2", json={"status": "answered", "answer": "Yes."}
    )
    assert earlier.status_code == 409
    assert "round 1" in earlier.json()["detail"]
    assert not (directory / "responses.json").exists()

    current = client.put(
        f"/clarifications/{_ROUND_2}/responses/q4", json={"status": "answered", "answer": "Ten."}
    )
    assert current.status_code == 200
    assert _statuses(current.json())["q2"] == "dont_know"  # unchanged round-1 answer
    assert _responses(directory) == {"q4": ("answered", "Ten.")}


# --- Invalid requests and files ---


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"status": "answered"}, id="answered-without-text"),
        pytest.param({"status": "answered", "answer": "   "}, id="answered-blank-text"),
        pytest.param({"status": "dont_know", "answer": "Maybe"}, id="dont-know-with-text"),
        pytest.param({"status": "deferred", "answer": "Later"}, id="deferred-with-text"),
        pytest.param({"status": None, "answer": "Text"}, id="unanswered-with-text"),
        pytest.param({"status": "maybe"}, id="unknown-status"),
        pytest.param({"answer": "No status"}, id="missing-status"),
        pytest.param({"status": "dont_know", "note": "x"}, id="unexpected-field"),
    ],
)
def test_invalid_answers_are_rejected_without_writing(
    runs: Path, client: TestClient, payload: dict[str, Any]
) -> None:
    directory = _round_1(runs)
    before = _digest(directory)

    response = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json=payload)

    assert response.status_code == 422
    assert _digest(directory) == before


def test_unknown_batch_and_question_are_not_found(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)

    assert client.get("/clarifications/clarify-20990101T000000Z-ffffff").status_code == 404
    unknown = client.put(f"/clarifications/{_ROUND_1}/responses/q9", json={"status": "dont_know"})
    assert unknown.status_code == 404
    assert not (directory / "responses.json").exists()


def test_invalid_response_file_is_reported_and_never_replaced(
    runs: Path, client: TestClient
) -> None:
    directory = _round_1(runs)
    broken = '{"clarification_id": "other", "responses": []}'
    (directory / "responses.json").write_text(broken)

    view = client.get(f"/clarifications/{_ROUND_1}")
    save = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": "dont_know"})

    assert view.status_code == save.status_code == 409
    assert "different clarification batch" in save.json()["detail"]
    assert "not changed" in save.json()["detail"]
    assert (directory / "responses.json").read_text() == broken


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{not json", id="unreadable"),
        pytest.param(json.dumps({"clarification_id": "x"}), id="wrong-shape"),
    ],
)
def test_invalid_artifact_is_reported(runs: Path, client: TestClient, content: str) -> None:
    directory = _round_1(runs)
    (directory / "clarification.json").write_text(content)

    assert client.get(f"/clarifications/{_ROUND_1}").status_code == 409
    save = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": "dont_know"})
    assert save.status_code == 409
    assert not (directory / "responses.json").exists()


def test_failed_batch_is_reported(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    data = json.loads((directory / "clarification.json").read_text())
    (directory / "clarification.json").write_text(json.dumps({**data, "status": "failed"}))

    response = client.get(f"/clarifications/{_ROUND_1}")

    assert response.status_code == 409
    assert "failed question-generation call" in response.json()["detail"]


# --- Filesystem boundary ---


@pytest.mark.parametrize(
    "batch_id",
    [
        "%2E%2E",
        "..%2F..%2Fetc",
        "clarify-x%2F..%2F..%2Fsecret",
        "clarify-..",
        "clarify-*",
        "team-20260101T000000Z-abc123",
        "%2Ftmp%2Fclarify-x",
    ],
)
def test_batch_ids_cannot_name_paths(runs: Path, client: TestClient, batch_id: str) -> None:
    _round_1(runs)

    assert client.get(f"/clarifications/{batch_id}").status_code == 404
    save = client.put(f"/clarifications/{batch_id}/responses/q1", json={"status": "dont_know"})
    assert save.status_code == 404


def test_batch_symlinked_outside_the_runs_directory_is_refused(
    tmp_path: Path, runs: Path, client: TestClient
) -> None:
    outside = _round_1(tmp_path / "elsewhere", name="clarify-outside")
    (runs / _RUN).mkdir(parents=True)
    (runs / _RUN / "clarify-20260101T000300Z-ccc333").symlink_to(outside)

    assert client.get("/clarifications/clarify-20260101T000300Z-ccc333").status_code == 403
    save = client.put(
        "/clarifications/clarify-20260101T000300Z-ccc333/responses/q1",
        json={"status": "dont_know"},
    )
    assert save.status_code == 403
    assert not (outside / "responses.json").exists()


def test_response_file_symlinked_outside_is_refused(
    tmp_path: Path, runs: Path, client: TestClient
) -> None:
    directory = _round_1(runs)
    outside = tmp_path / "elsewhere.json"
    outside.write_text("keep me")
    (directory / "responses.json").symlink_to(outside)

    save = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": "dont_know"})

    assert save.status_code == 403
    assert outside.read_text() == "keep me"


def test_response_file_symlinked_inside_the_runs_directory_is_refused(
    runs: Path, client: TestClient
) -> None:
    directory = _round_1(runs)
    (directory / "responses.json").symlink_to(directory / "clarification.json")
    before = _digest(directory)

    save = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": "dont_know"})

    assert save.status_code == 403
    assert client.get(f"/clarifications/{_ROUND_1}").status_code == 403
    assert _digest(directory) == before


@pytest.mark.parametrize("target", ["clarification.json", "../outside.json"])
def test_planted_temporary_file_symlink_is_never_written_through(
    runs: Path, client: TestClient, target: str
) -> None:
    # Regression: saves used a fixed `responses.json.tmp`, so a symlink with that name
    # pointing at the artifact made a save overwrite the artifact.
    directory = _round_1(runs)
    (runs / _RUN / "outside.json").write_text("keep me")
    (directory / "responses.json.tmp").symlink_to(directory / target)
    before = _digest(directory)
    outside_before = (runs / _RUN / "outside.json").read_text()

    save = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": "dont_know"})

    assert save.status_code == 200
    assert _digest(directory)["clarification.json"] == before["clarification.json"]
    assert (runs / _RUN / "outside.json").read_text() == outside_before
    assert (directory / "responses.json.tmp").is_symlink()  # left alone, not followed
    assert not (directory / "responses.json").is_symlink()
    assert _responses(directory)["q1"] == ("dont_know", None)
    assert sorted(p.name for p in directory.glob(".*.tmp")) == []  # no leftovers


@pytest.mark.parametrize("name", ["responses.json", "clarification.json"])
def test_invalid_utf8_is_an_actionable_invalid_file_error(
    runs: Path, client: TestClient, name: str
) -> None:
    directory = _round_1(runs)
    (directory / name).write_bytes(b'{"clarification_id": "\xff\xfe"}')
    before = _digest(directory)

    view = client.get(f"/clarifications/{_ROUND_1}")
    save = client.put(f"/clarifications/{_ROUND_1}/responses/q1", json={"status": "dont_know"})

    assert view.status_code == save.status_code == 409
    assert "could not be read as JSON" in save.json()["detail"]
    assert _digest(directory) == before


def test_eval_copies_are_found_and_duplicate_names_are_ambiguous(
    runs: Path, client: TestClient
) -> None:
    _round_1(runs, name="clarify-research-20261003T100555Z", parent="evals")
    _round_1(runs, name=_ROUND_1, parent="evals/team-pet-before-travel")
    assert client.get("/clarifications/clarify-research-20261003T100555Z").status_code == 200

    _round_1(runs)  # the same folder name under another run
    response = client.get(f"/clarifications/{_ROUND_1}")
    assert response.status_code == 409
    assert "more than one" in response.json()["detail"].lower()


def test_health_is_unchanged(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


# --- Assessment (mocked model) ---

_MODEL = "claude-test-model"
_SECRET = "MODEL-OUTPUT-MUST-NOT-LEAK"
_INSIGHT = {
    "blind_spot": "The separation space is still unknown.",
    "why_it_mattered": "An incomplete introduction needs a fallback.",
    "status": "open",
    "status_explanation": "The user doesn't know which room would work.",
}
_ROUND_1_REPORT: dict[str, Any] = {
    "remaining_considerations": [
        {
            "title": "Separation space",
            "observation": "No room is named.",
            "why_it_matters": "The cats may need to be apart.",
            "source_finding_ids": ["e1-f2"],
            "question_ids": ["q2"],
        }
    ],
    "addressed_concerns": [
        {"summary": "Kitten age is known.", "source_finding_ids": ["e3-f1"], "question_ids": ["q1"]}
    ],
    "unresolved_uncertainties": [],
    "takeaway": _INSIGHT,
}
_ROUND_2_REPORT: dict[str, Any] = {
    "remaining_considerations": [],
    "addressed_concerns": [
        {"summary": "Departure age known.", "source_finding_ids": ["e3-f1"], "question_ids": ["q4"]}
    ],
    "unresolved_uncertainties": [],
    "takeaway": {**_INSIGHT, "status": "addressed"},
}
_CONFIRM = {"confirm_paid_request": True}


def _message(text: str, stop_reason: StopReason = "end_turn") -> Message:
    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model=f"{_MODEL}-20260101",
        content=[TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=900, output_tokens=300),
    )


class _FakeModel:
    """Records each request; can hold the response until `release` is set."""

    def __init__(self) -> None:
        self.outcomes: list[Message] = []
        self.calls: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        assert self.outcomes, "unexpected model call"
        return self.outcomes.pop(0)

    def user_content(self) -> str:
        content: str = self.calls[-1]["messages"][0]["content"]
        return content


class _FakeClient:
    def __init__(self, model: _FakeModel) -> None:
        self.messages = model

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> _FakeModel:
    fake = _FakeModel()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: _FakeClient(fake))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", _MODEL)
    return fake


def _answer(client: TestClient, batch: str, qid: str, body: dict[str, Any]) -> None:
    assert client.put(f"/clarifications/{batch}/responses/{qid}", json=body).status_code == 200


def _answer_round_1(client: TestClient) -> None:
    _answer(client, _ROUND_1, "q1", {"status": "answered", "answer": "Ten weeks; one vaccine."})
    _answer(client, _ROUND_1, "q2", {"status": "dont_know"})
    _answer(client, _ROUND_1, "q3", {"status": "deferred"})


def _reassessments(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(directory.glob("reassessment-*.json"))]


def test_assessment_saves_and_returns_the_current_assessment(
    runs: Path, client: TestClient, model: _FakeModel
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    responses_before = (directory / "responses.json").read_bytes()
    model.outcomes.append(_message(json.dumps(_ROUND_1_REPORT)))

    response = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM)

    assert response.status_code == 200
    body = response.json()
    assert body["round"] == 1
    assert body["conclusion_status"] == "current_assessment"
    assert body["report"]["takeaway"] == _INSIGHT
    assert body["closing_note"] is None  # a concern remains
    assert body["answers_changed_during_assessment"] is False
    assert body["used_research"] is False
    assert [(r["question_id"], r["status"]) for r in body["assessed_responses"]] == [
        ("q1", "answered"),
        ("q2", "dont_know"),
        ("q3", "deferred"),
    ]
    assert len(model.calls) == 1  # one request, no retries
    assert "Ten weeks; one vaccine." in model.user_content()
    (saved,) = _reassessments(directory)
    assert saved["reassessment_id"] == body["reassessment_id"]
    assert saved["status"] == "success"
    assert saved["conclusion_status"] == "current_assessment"
    assert saved["recorded_responses"][0]["answer"] == "Ten weeks; one vaccine."
    assert (directory / "responses.json").read_bytes() == responses_before


@pytest.mark.parametrize(
    "payload", [{}, {"confirm_paid_request": False}, {"confirm_paid_request": True, "x": 1}]
)
def test_assessment_requires_explicit_confirmation(
    runs: Path, client: TestClient, model: _FakeModel, payload: dict[str, Any]
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)

    response = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=payload)

    assert response.status_code == 422
    assert model.calls == [] and _reassessments(directory) == []


@pytest.mark.parametrize("answers", ["none", "all-deferred"])
def test_nothing_usable_to_assess_makes_no_call(
    runs: Path, client: TestClient, model: _FakeModel, answers: str
) -> None:
    directory = _round_1(runs)
    if answers == "all-deferred":
        for qid in ("q1", "q2", "q3"):
            _answer(client, _ROUND_1, qid, {"status": "deferred"})

    response = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM)

    assert response.status_code == 409
    assert "Nothing to assess" in response.json()["detail"]
    assert "No request was made" in response.json()["detail"]
    assert model.calls == [] and _reassessments(directory) == []


def test_missing_model_configuration_makes_no_call(
    runs: Path, client: TestClient, model: _FakeModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    response = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM)

    assert response.status_code == 503
    assert "No request was made" in response.json()["detail"]
    assert "ANTHROPIC" not in response.json()["detail"]
    assert model.calls == [] and _reassessments(directory) == []


@pytest.mark.parametrize(
    ("text", "stop_reason", "reason"),
    [
        (f'{{"remaining_considerations": ["{_SECRET}', "max_tokens", "cut off"),
        (f"Not JSON: {_SECRET}", "end_turn", "expected assessment format"),
    ],
)
def test_failed_assessment_is_saved_and_explained_without_model_text(
    runs: Path,
    client: TestClient,
    model: _FakeModel,
    text: str,
    stop_reason: StopReason,
    reason: str,
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    model.outcomes.append(_message(text, stop_reason))

    response = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert reason in detail and "nothing was retried" in detail
    assert _SECRET not in detail
    assert len(model.calls) == 1
    (saved,) = _reassessments(directory)
    assert saved["status"] == "failed" and saved["report"] is None
    assert f"reassessment-{saved['reassessment_id']}.json" in detail
    assert _responses(directory)["q1"] == ("answered", "Ten weeks; one vaccine.")


def test_one_assessment_at_a_time_and_answers_are_frozen_while_it_runs(
    runs: Path, client: TestClient, model: _FakeModel
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    model.outcomes.append(_message(json.dumps(_ROUND_1_REPORT)))
    model.release.clear()
    first: dict[str, Any] = {}

    def run_first() -> None:
        response = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM)
        first.update(status=response.status_code, body=response.json())

    thread = threading.Thread(target=run_first)
    thread.start()
    assert model.started.wait(5)
    try:
        # A second request (another tab) is refused without a call.
        second = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM)
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
        # Answers can't change through the API while it runs.
        change = client.put(
            f"/clarifications/{_ROUND_1}/responses/q2",
            json={"status": "answered", "answer": "The spare room."},
        )
        assert change.status_code == 409
        assert "Nothing was saved" in change.json()["detail"]
        assert client.get(f"/clarifications/{_ROUND_1}").json()["assessment_running"] is True
        # An edit outside the API (by hand or the CLI) can't be prevented; it's reported.
        data = json.loads((directory / "responses.json").read_text())
        data["responses"][1].update(status="answered", answer="Edited by hand.")
        (directory / "responses.json").write_text(json.dumps(data))
    finally:
        model.release.set()
        thread.join(5)

    assert first["status"] == 200
    assert len(model.calls) == 1
    assert first["body"]["answers_changed_during_assessment"] is True
    assessed = {r["question_id"]: r["status"] for r in first["body"]["assessed_responses"]}
    assert assessed["q2"] == "dont_know"  # the snapshot taken at the start
    (saved,) = _reassessments(directory)
    assert saved["recorded_responses"][1]["status"] == "dont_know"
    # Afterwards, answers can change again and nothing is marked as running.
    assert client.get(f"/clarifications/{_ROUND_1}").json()["assessment_running"] is False
    _answer(client, _ROUND_1, "q3", {"status": "dont_know"})


def test_round_2_assessment_keeps_lineage_and_carried_research(
    runs: Path, client: TestClient, model: _FakeModel
) -> None:
    research: dict[str, Any] = {
        "research_id": "research-20260101T000000Z-fixture",
        "research_file": "research-fixture.json",
        "sources": [{"id": "s1", "url": "https://example.org/fictional"}],
        "questions": [],
    }
    directory = _round_2(runs, prior_research=research)
    _answer(client, _ROUND_2, "q4", {"status": "answered", "answer": "Eleven weeks."})
    model.outcomes.append(_message(json.dumps(_ROUND_2_REPORT)))

    response = client.post(f"/clarifications/{_ROUND_2}/reassessments", json=_CONFIRM)

    assert response.status_code == 200
    body = response.json()
    assert body["round"] == 2
    assert body["conclusion_status"] == "review_conclusion"
    assert body["used_research"] is True
    assert body["closing_note"] == clarification.NO_OPEN_CONCERNS_NOTE
    assert [(r["question_id"], r["round"], r["status"]) for r in body["assessed_responses"]] == [
        ("q1", 1, "answered"),
        ("q2", 1, "dont_know"),
        ("q3", 1, "deferred"),
        ("q4", 2, "answered"),
    ]
    content = model.user_content()
    assert "The kitten is 1.5 months old." in content  # round-1 answer carried forward
    assert "Eleven weeks." in content
    assert research["research_id"] in content
    (saved,) = _reassessments(directory)
    assert saved["clarification_round"] == 2
    assert saved["conclusion_status"] == "review_conclusion"
    assert [(r["question_id"], r["round"]) for r in saved["recorded_responses"]] == [
        ("q1", 1),
        ("q2", 1),
        ("q3", 1),
        ("q4", 2),
    ]
    assert saved["research"]["carried_forward_from_round"] == 1


def test_assessing_an_unknown_batch_makes_no_call(client: TestClient, model: _FakeModel) -> None:
    response = client.post(
        "/clarifications/clarify-20990101T000000Z-ffffff/reassessments", json=_CONFIRM
    )
    assert response.status_code == 404
    assert model.calls == []


# --- Reopening the latest saved assessment (read only; no model, no credentials) ---

_LATEST = "/clarifications/{}/assessments/latest"
_ROUND_1_ANSWERS = [
    {"question_id": "q1", "status": "answered", "answer": "Ten weeks; one vaccine."},
    {"question_id": "q2", "status": "dont_know", "answer": None},
    {"question_id": "q3", "status": "deferred", "answer": None},
]


def _save_assessment(
    directory: Path,
    reassessment_id: str,
    created_at: str,
    *,
    status: str = "success",
    clarification_id: str = "eval-kitten-before-holiday",
    report: dict[str, Any] | None = None,
    responses: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> Path:
    """A fictional saved `reassessment-*.json` in the shape `reassess` writes."""
    path = directory / f"reassessment-{reassessment_id}.json"
    data: dict[str, Any] = {
        "reassessment_id": reassessment_id,
        "created_at": created_at,
        "clarification_id": clarification_id,
        "status": status,
        "recorded_responses": _ROUND_1_ANSWERS if responses is None else responses,
        "unanswered_question_ids": [],
        "report": (_ROUND_1_REPORT if status == "success" else None) if report is None else report,
        **extra,
    }
    path.write_text(json.dumps(data))
    return path


def test_no_saved_assessment_is_an_empty_result(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    before = _digest(directory)

    response = client.get(_LATEST.format(_ROUND_1))

    assert response.status_code == 200
    assert response.json() == {"assessment": None, "unusable_files": 0}
    assert _digest(directory) == before  # reading creates and changes nothing


def test_latest_successful_assessment_is_chosen_by_saved_creation_time(
    runs: Path, client: TestClient
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    # File names sort the other way round: only `created_at` decides.
    _save_assessment(directory, "20260109T000000Z-aaa111", "2026-01-02T00:00:00+00:00")
    _save_assessment(directory, "20260101T000000Z-bbb222", "2026-01-03T00:00:00+00:00")
    # Equal times (one in another offset) fall back to the larger reassessment ID.
    _save_assessment(directory, "20260101T000000Z-ccc333", "2026-01-03T01:00:00+01:00")
    before = _digest(directory)

    body = client.get(_LATEST.format(_ROUND_1)).json()

    view = body["assessment"]
    assert view["reassessment_id"] == "20260101T000000Z-ccc333"
    assert view["created_at"] == "2026-01-03T01:00:00+01:00"
    assert view["round"] == 1
    assert view["conclusion_status"] == "current_assessment"
    assert view["report"]["takeaway"] == _INSIGHT
    assert view["closing_note"] is None
    assert view["based_on_earlier_answers"] is False
    assert view["used_research"] is False
    assert [(r["question_id"], r["round"], r["status"]) for r in view["assessed_responses"]] == [
        ("q1", 1, "answered"),
        ("q2", 1, "dont_know"),
        ("q3", 1, "deferred"),
    ]
    assert body["unusable_files"] == 0
    assert _digest(directory) == before


def test_a_newer_failed_attempt_does_not_hide_the_latest_success(
    runs: Path, client: TestClient
) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    _save_assessment(directory, "20260101T000000Z-aaa111", "2026-01-01T00:00:00+00:00")
    _save_assessment(
        directory, "20260102T000000Z-bbb222", "2026-01-02T00:00:00+00:00", status="failed"
    )

    body = client.get(_LATEST.format(_ROUND_1)).json()

    assert body["assessment"]["reassessment_id"] == "20260101T000000Z-aaa111"
    assert body["unusable_files"] == 0  # a failed attempt isn't an unusable file


def test_only_failed_attempts_mean_no_assessment(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    _save_assessment(
        directory, "20260102T000000Z-bbb222", "2026-01-02T00:00:00+00:00", status="failed"
    )

    assert client.get(_LATEST.format(_ROUND_1)).json() == {"assessment": None, "unusable_files": 0}


def test_changed_answers_are_compared_by_recorded_content(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    _save_assessment(directory, "20260101T000000Z-aaa111", "2026-01-01T00:00:00+00:00")

    def earlier() -> bool:
        view = client.get(_LATEST.format(_ROUND_1)).json()["assessment"]
        flag: bool = view["based_on_earlier_answers"]
        return flag

    assert earlier() is True  # no answers saved now; the assessment used three
    _answer_round_1(client)
    assert earlier() is False
    _answer(client, _ROUND_1, "q2", {"status": "not_applicable"})
    assert earlier() is True
    _answer(client, _ROUND_1, "q2", {"status": "dont_know"})
    assert earlier() is False  # rewriting the same answers isn't a change, whatever the mtime
    _answer(client, _ROUND_1, "q1", {"status": "answered", "answer": "Eleven weeks."})
    assert earlier() is True


def test_invalid_and_foreign_files_are_skipped_and_counted(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    _answer_round_1(client)
    _save_assessment(directory, "20260101T000000Z-good11", "2026-01-01T00:00:00+00:00")
    newer = "2026-02-01T00:00:00+00:00"
    _save_assessment(directory, "20260201T000000Z-other1", newer, clarification_id="eval-other")
    _save_assessment(directory, "20260201T000000Z-naive1", "2026-02-01T00:00:00")
    _save_assessment(directory, "20260201T000000Z-badtime", "yesterday")
    _save_assessment(directory, "20260201T000000Z-round2", newer, clarification_round=2)
    _save_assessment(
        directory, "20260201T000000Z-cites1", newer,
        report={**_ROUND_1_REPORT, "unresolved_uncertainties": [
            {"summary": "x", "source_finding_ids": ["e9-f9"], "question_ids": []}
        ]},
    )  # fmt: skip
    _save_assessment(
        directory, "20260201T000000Z-qids11", newer,
        responses=[{"question_id": "q9", "status": "dont_know", "answer": None}],
    )  # fmt: skip
    _save_assessment(directory, "20260201T000000Z-noreport", newer, report={})
    renamed = _save_assessment(directory, "20260201T000000Z-renam1", newer)
    renamed.rename(directory / "reassessment-20260201T000000Z-other9.json")
    (directory / "reassessment-20260201T000000Z-notjson.json").write_text("{not json")
    (directory / "reassessment-20260201T000000Z-utf8.json").write_bytes(b"\xff\xfe")
    (directory / "reassessment-20260201T000000Z-list.json").write_text("[]")
    outside = runs.parent / "outside.json"
    _save_assessment(runs.parent, "20260201T000000Z-link11", newer).rename(outside)
    (directory / "reassessment-20260201T000000Z-link11.json").symlink_to(outside)

    response = client.get(_LATEST.format(_ROUND_1))

    assert response.status_code == 200
    body = response.json()
    assert body["assessment"]["reassessment_id"] == "20260101T000000Z-good11"
    assert body["unusable_files"] == 12
    assert "e9-f9" not in response.text and "outside" not in response.text


def test_only_unusable_files_mean_no_assessment(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    (directory / "reassessment-20260201T000000Z-notjson.json").write_text("{not json")

    body = client.get(_LATEST.format(_ROUND_1)).json()

    assert body == {"assessment": None, "unusable_files": 1}


@pytest.mark.parametrize("takeaway", ["Older prose takeaway.", None, "absent"])
def test_older_report_formats_are_shown_without_rewriting(
    runs: Path, client: TestClient, takeaway: str | None
) -> None:
    directory = _round_1(runs)
    report = {k: v for k, v in _ROUND_1_REPORT.items() if k != "takeaway"}
    if takeaway != "absent":
        report["takeaway"] = takeaway
    # Older files have no conclusion_status either.
    path = _save_assessment(directory, "20260101T000000Z-old111", "2026-01-01T00:00:00+00:00",
                            report=report)  # fmt: skip
    before = path.read_bytes()

    view = client.get(_LATEST.format(_ROUND_1)).json()["assessment"]

    assert view["reassessment_id"] == "20260101T000000Z-old111"
    assert view["report"]["takeaway"] == (None if takeaway == "absent" else takeaway)
    assert view["conclusion_status"] == "current_assessment"
    assert path.read_bytes() == before


def test_mismatched_conclusion_status_is_unusable(runs: Path, client: TestClient) -> None:
    directory = _round_1(runs)
    _save_assessment(directory, "20260101T000000Z-aaa111", "2026-01-01T00:00:00+00:00",
                     conclusion_status="review_conclusion")  # fmt: skip

    assert client.get(_LATEST.format(_ROUND_1)).json() == {"assessment": None, "unusable_files": 1}


def test_round_2_latest_assessment_uses_both_rounds(runs: Path, client: TestClient) -> None:
    directory = _round_2(runs)
    prior = json.loads(_REASSESSMENT_FIXTURE.read_text())["recorded_responses"]
    q4 = {"question_id": "q4", "status": "answered", "answer": "Eleven weeks."}
    tagged = [*({**r, "round": 1} for r in prior), {**q4, "round": 2}]
    _save_assessment(
        directory, "20260101T000000Z-r2aaa1", "2026-01-01T00:00:00+00:00",
        clarification_id=_ROUND_2, clarification_round=2, conclusion_status="review_conclusion",
        report=_ROUND_2_REPORT, responses=tagged,
    )  # fmt: skip
    # A round-1 assessment copied into the follow-up folder isn't this batch's.
    _save_assessment(directory, "20260102T000000Z-r1copy", "2026-01-02T00:00:00+00:00")
    # Nor is one whose round-1 answers differ from those the batch carries.
    altered = [{**tagged[0], "answer": "A different round-1 answer."}, *tagged[1:]]
    _save_assessment(
        directory, "20260102T000000Z-r2alt1", "2026-01-02T00:00:00+00:00",
        clarification_id=_ROUND_2, clarification_round=2, report=_ROUND_2_REPORT,
        responses=altered,
    )  # fmt: skip
    _answer(client, _ROUND_2, "q4", {"status": "answered", "answer": "Eleven weeks."})

    body = client.get(_LATEST.format(_ROUND_2)).json()

    view = body["assessment"]
    assert view["reassessment_id"] == "20260101T000000Z-r2aaa1"
    assert view["round"] == 2
    assert view["conclusion_status"] == "review_conclusion"
    assert view["closing_note"] == clarification.NO_OPEN_CONCERNS_NOTE
    assert view["based_on_earlier_answers"] is False
    assert [(r["question_id"], r["round"]) for r in view["assessed_responses"]] == [
        ("q1", 1),
        ("q2", 1),
        ("q3", 1),
        ("q4", 2),
    ]
    assert body["unusable_files"] == 2
    _answer(client, _ROUND_2, "q4", {"status": "dont_know"})
    assert client.get(_LATEST.format(_ROUND_2)).json()["assessment"]["based_on_earlier_answers"]


def test_research_citations_must_match_the_saved_research_link(
    runs: Path, client: TestClient
) -> None:
    directory = _round_1(runs)
    cited = {**_ROUND_1_REPORT, "takeaway": {**_INSIGHT, "research_source_ids": ["s1"]}}
    link = {"research_id": "research-x", "research_file": "research-x.json", "source_ids": ["s1"]}
    _save_assessment(directory, "20260101T000000Z-res111", "2026-01-01T00:00:00+00:00",
                     report=cited, research=link)  # fmt: skip
    # Cites research it never used.
    _save_assessment(directory, "20260102T000000Z-res222", "2026-01-02T00:00:00+00:00",
                     report=cited)  # fmt: skip

    body = client.get(_LATEST.format(_ROUND_1)).json()

    assert body["assessment"]["reassessment_id"] == "20260101T000000Z-res111"
    assert body["assessment"]["used_research"] is True
    assert body["unusable_files"] == 1


def test_an_assessment_made_here_is_reopened_after_reload(
    runs: Path, client: TestClient, model: _FakeModel
) -> None:
    _round_1(runs)
    _answer_round_1(client)
    model.outcomes.append(_message(json.dumps(_ROUND_1_REPORT)))
    made = client.post(f"/clarifications/{_ROUND_1}/reassessments", json=_CONFIRM).json()

    reopened = client.get(_LATEST.format(_ROUND_1)).json()["assessment"]

    assert len(model.calls) == 1  # reopening made no request
    assert reopened["reassessment_id"] == made["reassessment_id"]
    assert reopened["report"] == made["report"]
    assert reopened["assessed_responses"] == made["assessed_responses"]
    assert reopened["based_on_earlier_answers"] is False


def test_reopening_makes_no_request_and_needs_no_credentials(
    runs: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _round_1(runs)
    _save_assessment(directory, "20260101T000000Z-aaa111", "2026-01-01T00:00:00+00:00")

    def no_reassess(*_: Any, **__: Any) -> None:
        raise AssertionError("reopening must not reassess")

    monkeypatch.setattr("blindspot.batch_api.reassess", no_reassess)
    # anthropic.AsyncAnthropic already fails in every test (conftest), and no key is set.
    assert client.get(_LATEST.format(_ROUND_1)).status_code == 200


def test_reopening_reports_batch_errors_like_viewing(runs: Path, client: TestClient) -> None:
    assert client.get(_LATEST.format("clarify-20990101T000000Z-ffffff")).status_code == 404
    assert client.get(_LATEST.format("clarify-..")).status_code == 404
    directory = _round_1(runs)
    (directory / "responses.json").write_text("{not json")
    _save_assessment(directory, "20260101T000000Z-aaa111", "2026-01-01T00:00:00+00:00")
    assert client.get(_LATEST.format(_ROUND_1)).status_code == 409  # never overwritten
    (directory / "clarification.json").write_text("{not json")
    assert client.get(_LATEST.format(_ROUND_1)).status_code == 409
