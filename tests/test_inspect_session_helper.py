"""Tests for the inspect-session skill's read-only helper.

These check deterministic session traversal and structural checks on small
hand-built sessions: lineage (valid, missing, mismatched), carried-forward
answers (dropped, changed), current response files that changed, failed and
zero-question follow-ups, and read-only behaviour. They say nothing about the
quality of questions or reassessments.
"""

import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HELPER = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "inspect-session"
    / "inspect_session.py"
)


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("inspect_session", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

_USAGE = {"model_calls": 1, "input_tokens": 100, "output_tokens": 50, "usage_incomplete": False}
_Q1 = {"id": "q1", "question": "Q1?", "why_it_matters": "W", "source_finding_ids": ["e1-f1"]}
_Q2 = {"id": "q2", "question": "Q2?", "why_it_matters": "W", "source_finding_ids": ["e1-f2"]}
_Q3 = {"id": "q3", "question": "Q3?", "why_it_matters": "W", "source_finding_ids": ["e1-f2"]}
_R1_ANSWERS: list[dict[str, Any]] = [
    {"question_id": "q1", "status": "answered", "answer": "Six months of savings."},
    {"question_id": "q2", "status": "dont_know", "answer": None},
]
_R1_REPORT = {
    "remaining_considerations": [
        {"title": "T", "source_finding_ids": ["e1-f2"], "question_ids": ["q2"]}
    ],
    "addressed_concerns": [
        {"summary": "S", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]}
    ],
    "unresolved_uncertainties": [],
}


def _finding(label: str) -> dict[str, Any]:
    return {
        "title": label,
        "observation": "O",
        "why_it_matters": "W",
        "assumptions": [],
        "question_to_check": "?",
    }


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _base_artifact(clarification_id: str, questions: list[Any]) -> dict[str, Any]:
    return {
        "clarification_id": clarification_id,
        "status": "success",
        "source_run": {"directory": "(fictional)", "run_id": "x"},
        "plan": "Fictional plan.",
        "experts": [{"position": 1, "report": {"findings": [_finding("A"), _finding("B")]}}],
        "questions": questions,
        "question_generation": {"model": "m", "usage": _USAGE, "elapsed_seconds": 1.0},
    }


def _reassessment(
    rid: str, clarification_id: str, artifact: Path, responses: list[Any], report: Any, **extra: Any
) -> dict[str, Any]:
    return {
        "reassessment_id": rid,
        "clarification_id": clarification_id,
        "clarification_file": str(artifact),
        "responses_file": str(artifact.parent / "responses.json"),
        "status": "success" if report is not None else "failed",
        "recorded_responses": responses,
        "unanswered_question_ids": [],
        "report": report,
        "model": "m",
        "usage": _USAGE,
        "elapsed_seconds": 2.0,
        **extra,
    }


def _session(
    tmp_path: Path,
    *,
    r2_questions: list[Any] | None = None,
    r2_status: str = "success",
    with_r2_reassessment: bool = True,
) -> dict[str, Path]:
    """Round 1 with two reassessments (A in lineage, newer B unrelated) plus a round-2 batch."""
    r1 = tmp_path / "runs" / "team-x" / "clarify-r1"
    r1_artifact = r1 / "clarification.json"
    _write(r1_artifact, _base_artifact("batch-1", [_Q1, _Q2]))
    _write(r1 / "responses.json", {"clarification_id": "batch-1", "responses": _R1_ANSWERS})
    a = r1 / "reassessment-20260101T000000Z-aaaaaa.json"
    _write(a, _reassessment("A", "batch-1", r1_artifact, _R1_ANSWERS, _R1_REPORT))
    b = r1 / "reassessment-20260102T000000Z-bbbbbb.json"  # newer, not in the lineage
    other = [{"question_id": "q1", "status": "deferred", "answer": None}, _R1_ANSWERS[1]]
    _write(b, _reassessment("B", "batch-1", r1_artifact, other, _R1_REPORT))

    r2 = tmp_path / "runs" / "team-x" / "clarify-r2"
    r2_artifact = r2 / "clarification.json"
    questions = [_Q3] if r2_questions is None else r2_questions
    artifact = {
        **_base_artifact("batch-2", questions if r2_status == "success" else []),
        "status": r2_status,
        "round": 2,
        "previous": {
            "clarification_id": "batch-1",
            "clarification_file": str(r1_artifact),
            "reassessment_id": "A",
            "reassessment_file": str(a),
        },
        "prior_questions": [_Q1, _Q2],
        "prior_responses": _R1_ANSWERS,
        "prior_unanswered_question_ids": [],
        "prior_reassessment": _R1_REPORT,
    }
    _write(r2_artifact, artifact)
    paths = {"r1_artifact": r1_artifact, "a": a, "b": b, "r2_artifact": r2_artifact}
    if r2_status == "success" and questions:
        r2_answers = [{"question_id": "q3", "status": "answered", "answer": "Fitted."}]
        _write(r2 / "responses.json", {"clarification_id": "batch-2", "responses": r2_answers})
        if with_r2_reassessment:
            c = r2 / "reassessment-20260103T000000Z-cccccc.json"
            recorded = [{**r, "round": 1} for r in _R1_ANSWERS] + [{**r2_answers[0], "round": 2}]
            report = {
                **_R1_REPORT,
                "addressed_concerns": [
                    {"summary": "S", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]},
                    {"summary": "S", "source_finding_ids": ["e1-f2"], "question_ids": ["q3"]},
                ],
            }
            _write(
                c,
                _reassessment("C", "batch-2", r2_artifact, recorded, report, clarification_round=2),
            )
            paths["c"] = c
    return paths


def _review(path: Path) -> str:
    return "\n".join(helper.review(helper.resolve(path)))


def _edit(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


# --- Lineage ---


def test_valid_two_round_session_follows_lineage_not_newness(tmp_path: Path) -> None:
    paths = _session(tmp_path)

    out = _review(paths["c"])

    assert f"round 1 reassessment: {paths['a']}" in out  # A by lineage, not the newer B
    assert paths["b"].name not in out
    assert "carried into the round-2 artifact: same" in out
    assert "used: round-1 answers same; round 2 {q3: answered}" in out
    assert "round 2 (new): q3->['e1-f2']" in out
    assert "STRUCTURAL DEFECTS: none" in out
    assert "LIMITATIONS: none" in out
    # Each call appears once: two generations and two reassessments.
    assert out.count("calls 1, in 100, out 50") == 4


def test_missing_parent_is_a_limitation(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(
        paths["r2_artifact"],
        lambda d: d["previous"].update(reassessment_file=str(tmp_path / "gone.json")),
    )

    out = _review(paths["c"])

    assert "round-1 reassessment named by lineage not found" in out
    assert "round 1 reassessment: unavailable" in out
    assert "carried into the round-2 artifact (unverified)" in out


def test_missing_lineage_block_is_a_limitation(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(paths["r2_artifact"], lambda d: d.pop("previous"))

    out = _review(paths["c"])

    assert "round-2 artifact has no `previous` lineage" in out


def test_mismatched_parent_is_a_defect(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(paths["r2_artifact"], lambda d: d["previous"].update(reassessment_id="Z"))

    out = _review(paths["c"])

    assert "lineage reassessment_id does not match the round-1 reassessment" in out


# --- Carried-forward answers ---


def test_dropped_or_changed_carried_forward_answers_are_defects(tmp_path: Path) -> None:
    paths = _session(tmp_path)

    def tamper(d: dict[str, Any]) -> None:
        d["prior_responses"] = [{**_R1_ANSWERS[0], "answer": "Changed later."}]  # q2 dropped

    _edit(paths["r2_artifact"], tamper)

    out = _review(paths["c"])

    assert "round-2 artifact prior_responses: earlier answers dropped: ['q2']" in out
    assert (
        "round-2 artifact prior_responses: earlier answers changed (status or text): ['q1']" in out
    )


def test_round_2_reassessment_must_use_the_carried_answers(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(
        paths["c"],
        lambda d: d["recorded_responses"][1].update(status="answered", answer="Now yes."),
    )

    out = _review(paths["c"])

    assert "round-1 answers: earlier answers changed (status or text): ['q2']" in out
    assert "addressed by a non-answer" not in out


def test_addressed_by_a_carried_non_answer_is_a_defect(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(
        paths["c"],
        lambda d: d["report"]["addressed_concerns"].append(
            {"summary": "S", "source_finding_ids": ["e1-f2"], "question_ids": ["q2"]}  # dont_know
        ),
    )

    out = _review(paths["c"])

    assert "addressed by a non-answer: ['q2']" in out


def test_changed_current_response_file_is_noted_not_used(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(
        paths["r1_artifact"].parent / "responses.json",
        lambda d: d["responses"][0].update(answer="Edited."),
    )

    out = _review(paths["c"])

    assert "responses.json has CHANGED since it was recorded" in out
    assert "carried into the round-2 artifact: same" in out  # still compared with the record
    assert "STRUCTURAL DEFECTS: none" in out


# --- Outcomes ---


def test_failed_followup(tmp_path: Path) -> None:
    paths = _session(tmp_path, r2_status="failed")

    out = _review(paths["r2_artifact"].parent)

    assert "round 2 follow-up generation: failed (0 questions)" in out
    assert "round 2 reassessment: none" in out


def test_zero_question_followup(tmp_path: Path) -> None:
    paths = _session(tmp_path, r2_questions=[])

    out = _review(paths["r2_artifact"])

    assert "success, zero questions (no useful follow-up" in out
    assert "round 2 reassessment: none" in out


def test_round_1_only_session_and_linked_followups(tmp_path: Path) -> None:
    paths = _session(tmp_path)

    out = _review(paths["a"])

    assert "round 2 artifact" not in out
    assert "follow-up batches whose lineage names this reassessment: ['clarify-r2']" in out
    assert "STRUCTURAL DEFECTS: none" in out


def test_ambiguous_round_1_batch_chooses_nothing(tmp_path: Path) -> None:
    paths = _session(tmp_path)

    out = _review(paths["r1_artifact"].parent)

    assert "2 reassessments of this round-1 batch exist" in out
    assert "None was chosen" in out
    assert "round 1 reassessment: unavailable" in out


def test_helper_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths = _session(tmp_path)
    root = tmp_path / "runs"

    def digest() -> dict[str, str]:
        return {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    before = digest()
    assert helper.main([str(paths["c"])]) == 0
    assert digest() == before
    assert "Session artifacts" in capsys.readouterr().out


# --- Takeaway and conclusion lifecycle ---


def test_takeaway_state_reports_legacy_and_new_reports(tmp_path: Path) -> None:
    paths = _session(tmp_path)

    # The round-2 reassessment is in the new format; round 1 stays legacy.
    def add_takeaway(d: dict[str, Any]) -> None:
        d["report"]["takeaway"] = "The answers address the runway concern."
        d["conclusion_status"] = "review_conclusion"

    _edit(paths["c"], add_takeaway)

    out = _review(paths["c"])

    assert (
        "no takeaway (legacy report, saved before takeaways); conclusion_status=unrecorded" in out
    )
    assert "takeaway present (39 chars; legacy text); conclusion_status=review_conclusion" in out
    assert "The answers address the runway concern." not in out  # prose is never printed


def test_zero_followup_points_to_the_available_conclusion(tmp_path: Path) -> None:
    paths = _session(tmp_path, r2_questions=[])
    _edit(
        paths["r2_artifact"],
        lambda d: d["question_generation"].update(
            no_useful_followup=True, conclusion_reassessment_file=str(paths["a"])
        ),
    )

    out = _review(paths["r2_artifact"])

    assert f"the conclusion available is the preceding reassessment {paths['a']}" in out
    assert "(not a new conclusion)" in out


def test_failed_reassessment_is_not_a_conclusion(tmp_path: Path) -> None:
    paths = _session(tmp_path)
    _edit(paths["c"], lambda d: d.update(status="failed", report=None, conclusion_status=None))

    out = _review(paths["c"])

    assert "no report (no takeaway, not a conclusion)" in out
