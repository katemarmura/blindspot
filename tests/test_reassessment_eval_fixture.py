"""Contract checks for the committed reassessment evaluation fixture.

These keep `evals/reassessment/kitten-before-holiday/` loadable by the real
`reassess` code path. They do not (and cannot) check reassessment quality;
see that directory's CHECKLIST.md for the manual, live assessment.
"""

import json
from pathlib import Path

import pytest

from blindspot import clarification

FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "reassessment" / "kitten-before-holiday"


def test_fixture_artifact_is_valid_and_self_consistent() -> None:
    artifact = clarification.load_artifact(FIXTURE / "clarification.json")

    assert artifact.status == "success"
    assert [q.id for q in artifact.questions] == ["q1", "q2", "q3"]
    assert "FICTIONAL" in artifact.plan
    saved = {
        sid: (ref["expert_position"], ref["finding_position"])
        for sid, ref in artifact.sources.items()
    }
    derived = {
        sid: (ref.expert_position, ref.finding_position)
        for sid, ref in artifact.context().sources.items()
    }
    assert saved == derived


RESPONSE_SETS = [
    "responses-baseline.json",
    "responses-arrangements.json",
    "responses-unmet-dependency.json",
    "responses-sufficient.json",
]


def _answers(name: str) -> dict[str, str]:
    data = json.loads((FIXTURE / name).read_text(encoding="utf-8"))
    return {r["question_id"]: r["answer"] for r in data["responses"]}


def test_fixture_contains_exactly_the_documented_answer_sets() -> None:
    assert sorted(p.name for p in FIXTURE.glob("responses-*.json")) == sorted(RESPONSE_SETS)
    checklist = (FIXTURE / "CHECKLIST.md").read_text(encoding="utf-8")
    for name in RESPONSE_SETS:
        assert f"`{name}`" in checklist


def test_every_finding_is_covered_by_a_question() -> None:
    # Case 6 (sufficient answers) is only possible if each finding can be answered.
    artifact = clarification.load_artifact(FIXTURE / "clarification.json")

    cited = {sid for q in artifact.questions for sid in q.source_finding_ids}

    assert cited == set(artifact.context().sources)


@pytest.mark.parametrize("name", RESPONSE_SETS)
def test_fixture_response_sets_are_valid_and_would_trigger_one_call(name: str) -> None:
    artifact = clarification.load_artifact(FIXTURE / "clarification.json")

    responses = clarification.load_responses(FIXTURE / name, artifact)

    assert [r.question_id for r in responses.recorded] == ["q1", "q2", "q3"]
    assert responses.unanswered == []
    assert responses.has_new_context


def test_answer_sets_differ_only_where_the_checklist_expects() -> None:
    baseline = _answers("responses-baseline.json")
    arrangements = _answers("responses-arrangements.json")
    unmet = _answers("responses-unmet-dependency.json")
    sufficient = _answers("responses-sufficient.json")

    # Cases 1-5 keep q1 identical, so vaccination timing stays unspecified (case 4).
    assert baseline["q1"] == arrangements["q1"] == unmet["q1"]
    # Case 5 changes only q3 relative to the arrangements set.
    assert unmet["q2"] == arrangements["q2"]
    assert unmet["q3"] != arrangements["q3"]
    # Cases 2 and 6 supply arrangements the baseline lacks.
    assert baseline["q2"] != arrangements["q2"]
    assert baseline["q3"] != arrangements["q3"]
    assert all(sufficient[q] != arrangements[q] for q in ("q1", "q2", "q3"))
