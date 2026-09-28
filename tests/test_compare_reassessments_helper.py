"""Tests for the compare-reassessments skill's read-only helper.

These check the helper's deterministic behaviour (pair selection, classification,
use of recorded responses, citation mapping, failed-call handling, read-only
operation). They say nothing about reassessment quality.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HELPER = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "compare-reassessments"
    / "compare_reassessments.py"
)


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("compare_reassessments", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _finding(label: str) -> dict[str, Any]:
    return {
        "title": label,
        "observation": f"{label} observation.",
        "why_it_matters": "W",
        "assumptions": [],
        "question_to_check": "Q?",
    }


def _clarification(plan: str = "Fictional plan.") -> dict[str, Any]:
    return {
        "clarification_id": "clarify-test",
        "plan": plan,
        "experts": [
            {"position": 1, "report": {"findings": [_finding("A"), _finding("B")]}},
            {"position": 2, "report": None},
            {"position": 3, "report": {"findings": [_finding("C")]}},
        ],
        "questions": [
            {"id": "q1", "source_finding_ids": ["e1-f1"]},
            {"id": "q2", "source_finding_ids": ["e1-f2", "e3-f1"]},
        ],
    }


_REPORT = {
    "remaining_considerations": [
        {"title": "T", "source_finding_ids": ["e1-f2"], "question_ids": ["q2"]}
    ],
    "addressed_concerns": [
        {"summary": "S", "source_finding_ids": ["e1-f1"], "question_ids": ["q1"]}
    ],
    "unresolved_uncertainties": [],
}


def _reassessment(
    *,
    prompt: str | None = "PROMPT v1",
    answers: dict[str, tuple[str, str | None]] | None = None,
    max_out: int = 15000,
    model: str = "model-x",
    report: dict[str, Any] | None = None,
    status: str = "success",
) -> dict[str, Any]:
    answers = (
        answers if answers is not None else {"q1": ("answered", "Yes."), "q2": ("dont_know", None)}
    )
    settings: dict[str, Any] = {
        "max_output_tokens": max_out,
        "git": {"revision": "abc", "dirty": False},
    }
    if prompt is not None:
        settings["system_prompt"] = prompt
    return {
        "clarification_id": "clarify-test",
        "status": status,
        "recorded_responses": [
            {"question_id": q, "status": s, "answer": a} for q, (s, a) in answers.items()
        ],
        "unanswered_question_ids": [],
        "report": (report if report is not None else _REPORT) if status == "success" else None,
        "requested_model": model,
        "model": f"{model}-dated" if status == "success" else None,
        "elapsed_seconds": 1.5,
        "usage": {
            "model_calls": 1,
            "input_tokens": 100,
            "output_tokens": 50,
            "usage_incomplete": False,
        },
        "settings": settings,
    }


def _batch(tmp_path: Path, *reassessments: dict[str, Any], plan: str = "Fictional plan.") -> Path:
    batch = tmp_path / "runs" / "team-x" / "clarify-20260101T000000Z-aaaaaa"
    batch.mkdir(parents=True)
    (batch / "clarification.json").write_text(json.dumps(_clarification(plan)))
    (batch / "responses.json").write_text(json.dumps({"responses": []}))
    for i, data in enumerate(reassessments):
        (batch / f"reassessment-2026010{i + 1}T000000Z-00000{i}.json").write_text(json.dumps(data))
    return batch


def _sides(batch: Path) -> tuple[Any, Any]:
    a, b = helper.latest_pair(batch)
    return helper.load_side(a), helper.load_side(b)


# --- Pair selection ---


def test_latest_pair_picks_the_two_newest_by_timestamped_name(tmp_path: Path) -> None:
    batch = _batch(tmp_path, _reassessment(), _reassessment(), _reassessment())

    a, b = helper.latest_pair(batch)

    assert (a.name, b.name) == (
        "reassessment-20260102T000000Z-000001.json",
        "reassessment-20260103T000000Z-000002.json",
    )


def test_batch_with_fewer_than_two_reassessments_is_rejected(tmp_path: Path) -> None:
    batch = _batch(tmp_path, _reassessment())

    with pytest.raises(SystemExit, match="Fewer than two"):
        helper.latest_pair(batch)


def test_newest_batch_ignores_batches_without_a_pair(tmp_path: Path) -> None:
    older = _batch(tmp_path, _reassessment(), _reassessment())
    lonely = tmp_path / "runs" / "team-y" / "clarify-20270101T000000Z-bbbbbb"
    lonely.mkdir(parents=True)
    (lonely / "reassessment-20270101T000000Z-999999.json").write_text("{}")

    assert helper.newest_batch(tmp_path / "runs") == older


# --- Classification ---


@pytest.mark.parametrize(
    ("b_kwargs", "expected"),
    [
        pytest.param({}, "same inputs and settings: run-to-run variation", id="same"),
        pytest.param(
            {"prompt": "PROMPT v2"}, "prompt change: same context and answers", id="prompt"
        ),
        pytest.param(
            {"answers": {"q1": ("answered", "Yes."), "q2": ("answered", "A bathroom.")}},
            "answer change: same context and prompt",
            id="answers",
        ),
        pytest.param({"max_out": 5000}, "settings change only", id="settings"),
        pytest.param(
            {"prompt": "PROMPT v2", "model": "model-y"},
            "multiple changes (prompt, settings): do not attribute to one cause",
            id="multiple",
        ),
        pytest.param(
            {"prompt": None},
            "same inputs and settings: run-to-run variation (unknown: prompt)",
            id="prompt-unknown",
        ),
    ],
)
def test_classification(tmp_path: Path, b_kwargs: dict[str, Any], expected: str) -> None:
    batch = _batch(tmp_path, _reassessment(), _reassessment(**b_kwargs))

    assert helper.classify(*_sides(batch)) == expected


def test_different_plans_are_flagged_as_different_investigations(tmp_path: Path) -> None:
    first = _batch(tmp_path / "one", _reassessment(), _reassessment())
    second = _batch(tmp_path / "two", _reassessment(), _reassessment(), plan="Another plan.")
    a = helper.load_side(helper.latest_pair(first)[1])
    b = helper.load_side(helper.latest_pair(second)[1])

    assert helper.classify(a, b).startswith("different investigations")


def test_recorded_responses_are_used_not_the_current_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = _batch(tmp_path, _reassessment(), _reassessment())
    # The user later edits the response file; the comparison must not see it.
    (batch / "responses.json").write_text(
        json.dumps({"responses": [{"question_id": "q2", "status": "answered", "answer": "New!"}]})
    )

    assert helper.main(["--batch", str(batch), "--no-prompt-diff"]) == 0

    out = capsys.readouterr().out
    assert "Classification: same inputs and settings" in out
    assert "q2: same (dont_know)" in out


# --- Output ---


def test_output_maps_citations_and_marks_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = {
        "remaining_considerations": [],
        "addressed_concerns": [
            {"summary": "S", "source_finding_ids": ["e1-f1", "e1-f2"], "question_ids": ["q1"]}
        ],
        "unresolved_uncertainties": [],
    }
    batch = _batch(tmp_path, _reassessment(), _reassessment(report=moved))

    helper.main(["--batch", str(batch)])

    out = capsys.readouterr().out
    assert "e1-f1: A A0           B A0\n" in out
    assert "e1-f2: A R0           B A0  *" in out
    assert "e3-f1: A -            B -\n" in out  # known but uncited on both sides
    assert "Section counts: A R=1 A=1 U=0 | B R=0 A=1 U=0" in out
    assert "Prompt (settings.system_prompt):\n  identical" in out
    assert "A: success 1.5s calls 1, in 100, out 50" in out


def test_prompt_diff_shows_changed_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = _batch(
        tmp_path,
        _reassessment(prompt="line one\nold rule"),
        _reassessment(prompt="line one\nnew rule"),
    )

    helper.main(["--batch", str(batch)])

    out = capsys.readouterr().out
    assert "    -old rule" in out
    assert "    +new rule" in out
    assert "line one" not in out  # unchanged lines are omitted


def test_structural_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    flawed = {
        "remaining_considerations": [
            {"title": "T", "source_finding_ids": ["e1-f2"], "question_ids": []}
        ],
        "addressed_concerns": [
            # q2 was "dont_know", so it cannot address anything; e2-f1 does not exist.
            {"summary": "S", "source_finding_ids": ["e2-f1"], "question_ids": ["q2"]}
        ],
        "unresolved_uncertainties": [
            {"summary": "U", "source_finding_ids": ["e1-f2"], "question_ids": []}
        ],
    }
    batch = _batch(tmp_path, _reassessment(), _reassessment(report=flawed))

    helper.main(["--batch", str(batch), "--no-prompt-diff"])

    out = capsys.readouterr().out
    assert "A: none" in out
    assert "B: UNRESOLVED in addressed_concerns: ['e2-f1']" in out
    assert "B: ADDRESSED BY NON-ANSWER: ['q2']" in out
    assert "B: findings cited in both R and U (check for repetition): ['e1-f2']" in out


def test_failed_call_is_not_an_empty_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = _batch(tmp_path, _reassessment(), _reassessment(status="failed"))

    helper.main(["--batch", str(batch), "--no-prompt-diff"])

    out = capsys.readouterr().out
    assert "Section counts: A R=1 A=1 U=0 | B no report" in out
    assert "B: no report (the call failed or was not saved): not an empty analysis" in out


def test_explicit_pair_and_argument_validation(tmp_path: Path) -> None:
    batch = _batch(tmp_path, _reassessment(), _reassessment())
    a, b = helper.latest_pair(batch)

    assert helper.main([str(a), str(b), "--no-prompt-diff"]) == 0
    with pytest.raises(SystemExit):
        helper.main([str(a)])


def test_helper_is_read_only(tmp_path: Path) -> None:
    batch = _batch(tmp_path, _reassessment(), _reassessment(prompt="PROMPT v2", status="failed"))

    def digest() -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(batch.iterdir())}

    before = digest()
    helper.main(["--batch", str(batch)])
    assert digest() == before
