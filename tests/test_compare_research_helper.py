"""Tests for the compare-research skill's read-only helper.

These check deterministic behaviour (classification, legacy fields, question matching by
finding IDs, performance arithmetic, failed runs, file errors, read-only operation). They
say nothing about research quality.
"""

import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HELPER = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "compare-research"
    / "compare_research.py"
)


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("compare_research", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

_LIMITS = {
    "max_research_questions": 3,
    "max_searches": 4,
    "max_fetches": 4,
    "max_output_tokens": 15000,
}


def _question(qid: str, findings: list[str], *, quotes: int = 1, status: str = "answered") -> Any:
    evidence = [{"source_id": "s1", "quote": "Q."} for _ in range(quotes)]
    return {
        "id": qid,
        "question": f"Question {qid}?",
        "source_finding_ids": findings,
        "reported_status": "answered",
        "status": status,
        "supported_findings": (
            [
                {
                    "source_statement": "S",
                    "interpretation": "I",
                    "applicability_limits": "L",
                    "evidence": evidence,
                }
            ]
            if quotes
            else []
        ),
        "missing_information": [],
        "conflict_notes": [],
        "limitations": [],
        "rejected_evidence": [] if quotes else [{"reason": "source_not_fetched"}],
    }


def _research(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "research_id": "20260101T000000Z-aaaaaa",
        "created_at": "2026-01-01T00:00:00+00:00",
        "status": "completed",
        "lineage": {"clarification_id": "clarify-x"},
        "input_snapshot": {
            "clarification_id": "clarify-x",
            "clarification_round": 1,
            "plan_sha256": "p" * 64,
            "findings_sha256": "f" * 64,
            "question_ids": ["q1", "q2"],
            "questions_sha256": "q" * 64,
            "recorded_responses": [
                {"question_id": "q1", "status": "answered", "answer": "Mid-January."},
                {"question_id": "q2", "status": "dont_know", "answer": None},
            ],
            "unanswered_question_ids": [],
        },
        "questions": [_question("r1", ["e1-f1"]), _question("r2", ["e1-f2"], quotes=0)],
        "sources": [{"id": "s1", "url": "https://gov.example/rules", "page_age": None}],
        "searches": [
            {
                "query": "rule days",
                "outcome": "ok",
                "results": [{"url": "https://gov.example/rules"}, {"url": "https://blog.example"}],
            }
        ],
        "fetches": [{"url": "https://gov.example/rules", "outcome": "ok", "source_id": "s1"}],
        "error": None,
        "diagnostics": None,
        "usage": {
            "model_calls": 1,
            "input_tokens": 1000,
            "output_tokens": 200,
            "usage_incomplete": False,
        },
        "server_tool_usage": {"web_search_requests": 4, "web_fetch_requests": 4},
        "call_diagnostics": {"elapsed_seconds": 100.0, "response_received": True},
        "timings": {"request_seconds": 100.0, "local_processing_seconds": 0.004},
        "settings": {
            "requested_model": "model-x",
            "system_prompt": "PROMPT v1",
            "research_reference_time": "2026-01-01T00:00:00+00:00",
            "allowed_domains": None,
            "limits": dict(_LIMITS),
            "git": {"revision": "abc", "dirty": False},
        },
    }
    for key, value in overrides.items():
        data[key] = value
    return data


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def _sides(tmp_path: Path, a: dict[str, Any], b: dict[str, Any]) -> tuple[Any, Any]:
    return (
        helper.load_side(_write(tmp_path, "research-a.json", a)),
        helper.load_side(_write(tmp_path, "research-b.json", b)),
    )


def _run(tmp_path: Path, a: dict[str, Any], b: dict[str, Any]) -> tuple[int, str]:
    pa = _write(tmp_path, "research-a.json", a)
    pb = _write(tmp_path, "research-b.json", b)
    return helper.main([str(pa), str(pb)]), ""


def _changed(**settings: Any) -> dict[str, Any]:
    data = _research()
    data["settings"].update(settings)
    return data


# --- Classification ---


def test_identical_configuration_is_run_to_run_variation(tmp_path: Path) -> None:
    a, b = _sides(tmp_path, _research(), _research())

    assert helper.classify(a, b) == "same inputs and configuration: run-to-run variation"
    assert helper.scope_changes(a, b) == []


def test_changed_prompt_is_a_single_change(tmp_path: Path) -> None:
    a, b = _sides(tmp_path, _research(), _changed(system_prompt="PROMPT v2"))

    assert helper.classify(a, b) == "single change (prompt)"


def test_changed_answers_are_detected(tmp_path: Path) -> None:
    other = _research()
    other["input_snapshot"]["recorded_responses"][1] = {
        "question_id": "q2",
        "status": "answered",
        "answer": "Remote work.",
    }
    a, b = _sides(tmp_path, _research(), other)

    assert helper.classify(a, b) == "single change (recorded answers)"


def test_changed_prompt_and_limits_flag_scope_and_multiple_causes(tmp_path: Path) -> None:
    limits = {**_LIMITS, "max_research_questions": 1, "max_searches": 2, "max_fetches": 2}
    a, b = _sides(tmp_path, _research(), _changed(system_prompt="PROMPT v2", limits=limits))

    assert helper.classify(a, b) == (
        "multiple changes (prompt, limits): do not attribute to one cause"
    )
    assert helper.scope_changes(a, b) == [
        "max_research_questions 3 -> 1",
        "max_searches 4 -> 2",
        "max_fetches 4 -> 2",
    ]


def test_different_plan_is_a_different_investigation(tmp_path: Path) -> None:
    other = _research()
    other["input_snapshot"]["plan_sha256"] = "x" * 64
    a, b = _sides(tmp_path, _research(), other)

    assert helper.classify(a, b).startswith("different investigations (plan differs)")


def test_missing_legacy_fields_are_unavailable_not_same(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = _research()
    del legacy["timings"]
    del legacy["settings"]["research_reference_time"]
    del legacy["input_snapshot"]["findings_sha256"]
    del legacy["input_snapshot"]["questions_sha256"]

    assert (
        helper.main(
            [str(_write(tmp_path, "a.json", legacy)), str(_write(tmp_path, "b.json", _research()))]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "unavailable: original findings, question wording" in out
    assert "original findings: unavailable (A saved: False, B saved: True" in out
    assert "reference time unavailable" in out
    # Request time falls back to the same measurement; local processing stays unavailable.
    assert "A request seconds: from call_diagnostics.elapsed_seconds (no timings)" in out
    assert re.search(r"local processing seconds +A unavailable +B +0\.004 +unavailable", out)


# --- Performance arithmetic ---


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (100.0, 40.5, "-59.500 (-59.5%)"),
        (4, 2, "-2 (-50.0%)"),
        (0, 1, "+1 (no %: A is zero)"),
        (0, 0, "+0 (no %: A is zero)"),
        (None, 5, "unavailable"),
        (5, None, "unavailable"),
    ],
)
def test_delta(a: float | None, b: float | None, expected: str) -> None:
    assert helper.delta(a, b) == expected


def test_incomplete_usage_is_unavailable_not_zero(tmp_path: Path) -> None:
    incomplete = _research()
    incomplete["usage"]["usage_incomplete"] = True
    no_tools = _research(server_tool_usage=None)
    a, b = _sides(tmp_path, incomplete, no_tools)

    ma, mb = helper.metrics(a), helper.metrics(b)
    assert ma["input tokens"] is None and ma["output tokens"] is None
    assert mb["searches (provider)"] is None and mb["fetches (provider)"] is None
    assert ma["searches (provider)"] == 4


def test_failed_run_has_no_evidence_counts_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = _research(
        status="failed",
        error="API error (500): boom",
        questions=[],
        sources=[],
        timings={"request_seconds": 2.5, "local_processing_seconds": None},
        diagnostics={"failure_stage": "research", "stop_reason": None},
    )
    a, b = _sides(tmp_path, _research(), failed)
    assert helper.metrics(b)["verified quotes"] is None  # unavailable, not zero

    helper.main([str(a.path), str(b.path)])

    out = capsys.readouterr().out
    assert "B: failed [stage=research, stop=None]" in out
    assert "error: API error (500): boom" in out
    assert "no evidence was produced: not an empty or reassuring result" in out
    assert re.search(r"verified quotes +A +1 +B unavailable +unavailable", out)


# --- Evidence and coverage ---


def test_reordered_questions_are_matched_by_finding_ids(tmp_path: Path) -> None:
    reordered = _research(
        questions=[_question("r1", ["e1-f2"], quotes=0), _question("r2", ["e1-f1"])]
    )
    a, b = _sides(tmp_path, _research(), reordered)

    assert helper.match_questions(a, b) == [
        ("r1", "r2", "same finding IDs"),
        ("r2", "r1", "same finding IDs"),
    ]


def test_narrowed_scope_shows_coverage_only_in_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    narrowed = _research(questions=[_question("r1", ["e1-f1", "e2-f1"])])
    a, b = _sides(tmp_path, _research(), narrowed)

    assert helper.match_questions(a, b) == [
        ("r1", "r1", "overlapping finding IDs"),
        ("r2", None, "only in A"),
    ]
    helper.main([str(a.path), str(b.path), "--no-prompt-diff"])
    out = capsys.readouterr().out
    assert "e1-f2: A researched B -  *" in out
    assert "search result URLs never fetched (leads, not evidence): 1 across 1 host(s)" in out
    assert "answered -> " not in out  # no downgrade when statuses match


def test_downgraded_status_is_shown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    downgraded = _research(
        questions=[_question("r1", ["e1-f1"], quotes=0, status="insufficient_evidence")]
    )
    a, b = _sides(tmp_path, _research(), downgraded)

    helper.main([str(a.path), str(b.path)])

    out = capsys.readouterr().out
    assert "status answered -> insufficient_evidence (downgraded)" in out
    assert "rejected {'source_not_fetched': 1}" in out


def test_prompt_diff_and_dirty_git_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a_data, b_data = _research(), _changed(system_prompt="PROMPT v1\nnew rule")
    for data in (a_data, b_data):
        data["settings"]["git"] = {"revision": "abc", "dirty": True}
    helper.main([str(_write(tmp_path, "a.json", a_data)), str(_write(tmp_path, "b.json", b_data))])

    out = capsys.readouterr().out
    assert "    +new rule" in out
    assert "the revision does not establish what ran" in out


# --- Files and safety ---


@pytest.mark.parametrize("problem", ["missing", "invalid_json", "not_research"])
def test_bad_files_are_reported_without_substitution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], problem: str
) -> None:
    good = _write(tmp_path, "research-good.json", _research())
    bad = tmp_path / "research-bad.json"
    if problem == "invalid_json":
        bad.write_text("{not json")
    elif problem == "not_research":
        bad.write_text(json.dumps({"reassessment_id": "x"}))

    assert helper.main([str(good), str(bad)]) == 2

    err = capsys.readouterr().err
    assert str(bad) in err
    assert "no other file was substituted" in err


def test_two_explicit_files_are_required(tmp_path: Path) -> None:
    path = _write(tmp_path, "research-a.json", _research())
    with pytest.raises(SystemExit):
        helper.main([str(path)])
    with pytest.raises(SystemExit):
        helper.main([])


def test_helper_is_read_only(tmp_path: Path) -> None:
    a = _write(tmp_path, "research-a.json", _research())
    b = _write(tmp_path, "research-b.json", copy.deepcopy(_changed(system_prompt="v2")))

    def digest() -> dict[str, str]:
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tmp_path.iterdir())
        }

    before = digest()
    helper.main([str(a), str(b)])
    assert digest() == before


# --- Regressions: explicit null versus missing, reference time, exact-first matching ---


def test_restricting_allowed_domains_is_a_change_not_variation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = _sides(tmp_path, _research(), _changed(allowed_domains=["gov.example"]))

    assert helper.classify(a, b) == "single change (allowed domains)"
    helper.compare(a, b)
    out = capsys.readouterr().out
    assert "allowed domains unrestricted (null)" in out
    assert "allowed domains ['gov.example']" in out


def test_missing_allowed_domains_is_unavailable_not_unrestricted(tmp_path: Path) -> None:
    legacy = _research()
    del legacy["settings"]["allowed_domains"]
    a, b = _sides(tmp_path, legacy, _research())

    assert helper.classify(a, b) == (
        "same inputs and configuration: run-to-run variation (unavailable: allowed domains)"
    )


def test_reference_time_difference_is_classified(tmp_path: Path) -> None:
    later = "2026-10-06T15:41:10+00:00"
    a, b = _sides(tmp_path, _research(), _changed(research_reference_time=later))
    assert helper.classify(a, b).startswith(
        "same inputs and configuration except the research reference time"
    )

    a, b = _sides(
        tmp_path, _research(), _changed(research_reference_time=later, system_prompt="v2")
    )
    assert helper.classify(a, b) == (
        "multiple changes (prompt, research reference time): do not attribute to one cause"
    )

    legacy = _research()
    del legacy["settings"]["research_reference_time"]
    a, b = _sides(tmp_path, legacy, _research())
    assert helper.classify(a, b) == (
        "same inputs and configuration: run-to-run variation (unavailable: research reference time)"
    )


def test_exact_match_is_not_consumed_by_an_earlier_overlap(tmp_path: Path) -> None:
    older = _research(questions=[_question("r1", ["e1-f1", "e1-f2"]), _question("r2", ["e1-f1"])])
    newer = _research(questions=[_question("r1", ["e1-f1"]), _question("r2", ["e1-f2", "e3-f1"])])
    a, b = _sides(tmp_path, older, newer)

    assert helper.match_questions(a, b) == [
        ("r1", "r2", "overlapping finding IDs"),
        ("r2", "r1", "same finding IDs"),
    ]
