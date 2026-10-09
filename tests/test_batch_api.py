"""The HTTP interface for viewing and answering a saved clarification batch.

Batches are copies of the committed kitten fixture in a temporary runs directory; a
round-2 batch is assembled from it by hand. No model, credentials, or network are used.
"""

import hashlib
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
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


def _round_2(runs: Path) -> Path:
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
        "..",
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
