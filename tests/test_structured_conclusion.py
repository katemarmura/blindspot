"""Structural validation, legacy loading and deterministic conclusion presentation."""

import pytest
from pydantic import ValidationError

from blindspot.clarification import render_conclusion
from blindspot.schemas import ConclusionInsight, ReassessmentReport, SavedReassessmentReport

INSIGHT = {
    "blind_spot": "No responder was available for three days.",
    "why_it_mattered": "Noticing a problem would not ensure an in-person response.",
    "status": "addressed",
    "status_explanation": "The reported sitter booking covers those days.",
}
SECTIONS = {
    "remaining_considerations": [],
    "addressed_concerns": [
        {"summary": "Coverage arranged.", "source_finding_ids": ["e1-f1"], "question_ids": ["q4"]}
    ],
    "unresolved_uncertainties": [],
}
CLOSING = "Based on your answers, no additional open concerns remain among those examined."


@pytest.mark.parametrize("field", ["blind_spot", "why_it_mattered", "status_explanation"])
def test_each_insight_text_is_required_and_nonblank(field: str) -> None:
    data = dict(INSIGHT)
    data.pop(field)
    with pytest.raises(ValidationError):
        ConclusionInsight.model_validate(data)
    with pytest.raises(ValidationError):
        ConclusionInsight.model_validate({**INSIGHT, field: "   "})


def test_status_and_extra_fields_are_restricted() -> None:
    with pytest.raises(ValidationError):
        ConclusionInsight.model_validate({**INSIGHT, "status": "safe"})
    with pytest.raises(ValidationError):
        ConclusionInsight.model_validate({**INSIGHT, "closing": "All safe!"})


def test_new_prose_rejected_legacy_prose_preserved() -> None:
    data = {**SECTIONS, "takeaway": "An older conclusion."}
    with pytest.raises(ValidationError):
        ReassessmentReport.model_validate(data)
    legacy = SavedReassessmentReport.model_validate(data)
    assert legacy.takeaway == "An older conclusion."
    assert render_conclusion(legacy) == "Legacy takeaway: An older conclusion."
    assert SavedReassessmentReport.model_validate(SECTIONS).takeaway is None


def test_empty_report_requires_null_instead_of_an_invented_insight() -> None:
    sections: dict[str, list[object]] = {key: [] for key in SECTIONS}
    empty = ReassessmentReport.model_validate({**sections, "takeaway": None})
    assert render_conclusion(empty) == CLOSING
    with pytest.raises(ValidationError):
        ReassessmentReport.model_validate({**sections, "takeaway": INSIGHT})
    with pytest.raises(ValidationError):
        ReassessmentReport.model_validate({**SECTIONS, "takeaway": None})


def test_render_uses_labels_and_adds_closing_only_without_open_items() -> None:
    report = ReassessmentReport.model_validate({**SECTIONS, "takeaway": INSIGHT})
    assert render_conclusion(report) == (
        f"Blind spot: {INSIGHT['blind_spot']}\n"
        f"Why it mattered: {INSIGHT['why_it_mattered']}\n"
        f"Status: Addressed. {INSIGHT['status_explanation']}\n{CLOSING}"
    )
    for status in ["open", "partly_addressed"]:
        open_report = ReassessmentReport.model_validate(
            {
                **SECTIONS,
                "unresolved_uncertainties": [
                    {
                        "summary": "Availability unknown.",
                        "question_ids": ["q4"],
                        "source_finding_ids": ["e1-f1"],
                    }
                ],
                "takeaway": {**INSIGHT, "status": status},
            }
        )
        assert CLOSING not in render_conclusion(open_report)
